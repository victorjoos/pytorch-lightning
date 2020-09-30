# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import io
import os
import re
import signal
from abc import ABC
from subprocess import call

import torch
import torch.distributed as torch_distrib

import pytorch_lightning
from pytorch_lightning import _logger as log
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning.core.lightning import LightningModule
from pytorch_lightning.loggers import LightningLoggerBase
from pytorch_lightning.overrides.data_parallel import LightningDataParallel, LightningDistributedDataParallel
from pytorch_lightning.utilities import AMPType, rank_zero_warn
from pytorch_lightning.utilities.cloud_io import atomic_save, get_filesystem
from pytorch_lightning.utilities.cloud_io import load as pl_load
from pytorch_lightning.utilities.upgrade_checkpoint import KEYS_MAPPING as DEPRECATED_CHECKPOINT_KEYS
from pytorch_lightning.accelerators.base_backend import Accelerator, DeviceType
from pytorch_lightning.utilities.exceptions import MisconfigurationException

try:
    from apex import amp
except ImportError:
    amp = None

try:
    from omegaconf import Container
except ImportError:
    OMEGACONF_AVAILABLE = False
else:
    OMEGACONF_AVAILABLE = True


class CheckpointConnector:

    def __init__(self, trainer):
        self.trainer = trainer

    def restore_weights(self, model: LightningModule):
        """
        We attempt to restore weights in this order:
        1. HPC weights.
        2. if no HPC weights restore checkpoint_path weights
        3. otherwise don't restore weights
        """
        on_gpu = self.trainer.on_device == DeviceType.GPU
        # clear cache before restore
        if on_gpu:
            torch.cuda.empty_cache()

        # if script called from hpc resubmit, load weights
        did_restore_hpc_weights = self.restore_hpc_weights_if_needed(model)

        # clear cache after restore
        if on_gpu:
            torch.cuda.empty_cache()

        if not did_restore_hpc_weights:
            if self.trainer.resume_from_checkpoint is not None:
                self.restore(self.trainer.resume_from_checkpoint, on_gpu=on_gpu)

        # wait for all to catch up
        self.trainer.accelerator_backend.barrier('TrainerIOMixin.restore_weights')

        # clear cache after restore
        if on_gpu:
            torch.cuda.empty_cache()

    def restore(self, checkpoint_path: str, on_gpu: bool):
        """
        Restore training state from checkpoint.
        Also restores all training state like:
        - epoch
        - callbacks
        - schedulers
        - optimizer
        """

        # if on_gpu:
        #     checkpoint = torch.load(checkpoint_path)
        # else:
        # load on CPU first
        checkpoint = pl_load(checkpoint_path, map_location=lambda storage, loc: storage)

        # load model state
        model = self.trainer.get_model()

        # load the state_dict on the model automatically
        model.load_state_dict(checkpoint['state_dict'])

        # give the datamodule a chance to load something
        if self.trainer.datamodule is not None:
            self.trainer.datamodule.on_load_checkpoint(checkpoint)
        # give model a chance to load something
        model.on_load_checkpoint(checkpoint)

        if on_gpu:
            model.cuda(self.trainer.root_gpu)

        # restore amp scaling
        if self.trainer.amp_backend == AMPType.NATIVE and 'native_amp_scaling_state' in checkpoint:
            self.trainer.scaler.load_state_dict(checkpoint['native_amp_scaling_state'])
        elif self.trainer.amp_backend == AMPType.APEX and 'amp_scaling_state' in checkpoint:
            amp.load_state_dict(checkpoint['amp_scaling_state'])

        # load training state (affects trainer only)
        self.restore_training_state(checkpoint)

    def restore_training_state(self, checkpoint):
        """
        Restore trainer state.
        Model will get its change to update
        :param checkpoint:
        :return:
        """
        if 'optimizer_states' not in checkpoint or 'lr_schedulers' not in checkpoint:
            raise KeyError(
                'Trying to restore training state but checkpoint contains only the model.'
                ' This is probably due to `ModelCheckpoint.save_weights_only` being set to `True`.'
            )

        if any([key in checkpoint for key in DEPRECATED_CHECKPOINT_KEYS]):
            raise ValueError(
                "The checkpoint you're attempting to load follows an"
                " outdated schema. You can upgrade to the current schema by running"
                " `python -m pytorch_lightning.utilities.upgrade_checkpoint --file model.ckpt`"
                " where `model.ckpt` is your checkpoint file."
            )

        # load callback states
        self.trainer.on_load_checkpoint(checkpoint)

        self.trainer.global_step = checkpoint['global_step']
        self.trainer.current_epoch = checkpoint['epoch']

        # crash if max_epochs is lower than the current epoch from the checkpoint
        if self.trainer.current_epoch > self.trainer.max_epochs:
            m = f"""
            you restored a checkpoint with current_epoch={self.trainer.current_epoch}
            but the Trainer(max_epochs={self.trainer.max_epochs})
            """
            raise MisconfigurationException(m)

        # Division deals with global step stepping once per accumulated batch
        # Inequality deals with different global step for odd vs even num_training_batches
        n_accum = 1 if self.trainer.accumulate_grad_batches is None else self.trainer.accumulate_grad_batches
        expected_steps = self.trainer.num_training_batches / n_accum
        if self.trainer.num_training_batches != 0 and self.trainer.global_step % expected_steps > 1:
            rank_zero_warn(
                "You're resuming from a checkpoint that ended mid-epoch. "
                "This can cause unreliable results if further training is done, "
                "consider using an end of epoch checkpoint. "
            )

        # restore the optimizers
        optimizer_states = checkpoint['optimizer_states']
        for optimizer, opt_state in zip(self.trainer.optimizers, optimizer_states):
            optimizer.load_state_dict(opt_state)

            # move optimizer to GPU 1 weight at a time
            # avoids OOM
            if self.trainer.root_gpu is not None:
                for state in optimizer.state.values():
                    for k, v in state.items():
                        if isinstance(v, torch.Tensor):
                            state[k] = v.cuda(self.trainer.root_gpu)

        # restore the lr schedulers
        lr_schedulers = checkpoint['lr_schedulers']
        for scheduler, lrs_state in zip(self.trainer.lr_schedulers, lr_schedulers):
            scheduler['scheduler'].load_state_dict(lrs_state)

    def restore_hpc_weights_if_needed(self, model: LightningModule):
        """If there is a set of hpc weights, use as signal to restore model."""
        did_restore = False

        # look for hpc weights
        folderpath = str(self.trainer.weights_save_path)
        fs = get_filesystem(folderpath)
        if fs.exists(folderpath):
            files = [os.path.basename(f) for f in fs.ls(folderpath)]
            hpc_weight_paths = [x for x in files if 'hpc_ckpt' in x]

            # if hpc weights exist restore model
            on_gpu = self.trainer.on_device == DeviceType.GPU
            if len(hpc_weight_paths) > 0:
                self.hpc_load(folderpath, on_gpu)
                did_restore = True
        return did_restore

    # ----------------------------------
    # PRIVATE OPS
    # ----------------------------------
    def hpc_save(self, folderpath: str, logger):
        # make sure the checkpoint folder exists
        folderpath = str(folderpath)  # because the tests pass a path object
        fs = get_filesystem(folderpath)
        fs.makedirs(folderpath, exist_ok=True)

        # save logger to make sure we get all the metrics
        logger.save()

        ckpt_number = self.max_ckpt_in_folder(folderpath) + 1

        fs.makedirs(folderpath, exist_ok=True)
        filepath = os.path.join(folderpath, f'hpc_ckpt_{ckpt_number}.ckpt')

        # give model a chance to do something on hpc_save
        model = self.trainer.get_model()
        checkpoint = self.dump_checkpoint()

        model.on_hpc_save(checkpoint)

        # do the actual save
        # TODO: fix for anything with multiprocess DP, DDP, DDP2
        try:
            atomic_save(checkpoint, filepath)
        except AttributeError as err:
            if LightningModule.CHECKPOINT_HYPER_PARAMS_KEY in checkpoint:
                del checkpoint[LightningModule.CHECKPOINT_HYPER_PARAMS_KEY]
            rank_zero_warn(
                'warning, `module_arguments` dropped from checkpoint.' f' An attribute is not picklable {err}'
            )
            atomic_save(checkpoint, filepath)

        return filepath

    def dump_checkpoint(self, weights_only: bool = False) -> dict:
        """Creating model checkpoint.

        Args:
            weights_only: saving model weights only

        Return:
             structured dictionary
        """
        checkpoint = {
            'epoch': self.trainer.current_epoch + 1,
            'global_step': self.trainer.global_step + 1,
            'pytorch-lightning_version': pytorch_lightning.__version__,
        }

        if not weights_only:

            # save callbacks
            callback_states = self.trainer.on_save_checkpoint()
            checkpoint['callbacks'] = callback_states

            # save optimizers
            optimizer_states = []
            for i, optimizer in enumerate(self.trainer.optimizers):
                optimizer_states.append(optimizer.state_dict())
            checkpoint['optimizer_states'] = optimizer_states

            # save lr schedulers
            lr_schedulers = []
            for scheduler in self.trainer.lr_schedulers:
                lr_schedulers.append(scheduler['scheduler'].state_dict())
            checkpoint['lr_schedulers'] = lr_schedulers

            # save native amp scaling
            if (self.trainer.amp_backend == AMPType.NATIVE and
                    self.trainer.on_device != DeviceType.TPU and
                    self.trainer.scaler is not None):
                checkpoint['native_amp_scaling_state'] = self.trainer.scaler.state_dict()
            elif self.trainer.amp_backend == AMPType.APEX:
                checkpoint['amp_scaling_state'] = amp.state_dict()

        # add the module_arguments and state_dict from the model
        model = self.trainer.get_model()

        checkpoint['state_dict'] = model.state_dict()

        if model.hparams:
            if hasattr(model, '_hparams_name'):
                checkpoint[LightningModule.CHECKPOINT_HYPER_PARAMS_NAME] = model._hparams_name
            # add arguments to the checkpoint
            if OMEGACONF_AVAILABLE:
                checkpoint[LightningModule.CHECKPOINT_HYPER_PARAMS_KEY] = model.hparams
                if isinstance(model.hparams, Container):
                    checkpoint[LightningModule.CHECKPOINT_HYPER_PARAMS_TYPE] = type(model.hparams)
            else:
                checkpoint[LightningModule.CHECKPOINT_HYPER_PARAMS_KEY] = dict(model.hparams)

        # give the model a chance to add a few things
        model.on_save_checkpoint(checkpoint)
        if self.trainer.datamodule is not None:
            self.trainer.datamodule.on_save_checkpoint(checkpoint)

        return checkpoint

    def hpc_load(self, folderpath, on_gpu):
        filepath = '{}/hpc_ckpt_{}.ckpt'.format(folderpath, self.max_ckpt_in_folder(folderpath))

        # load on CPU first
        checkpoint = torch.load(filepath, map_location=lambda storage, loc: storage)

        # load model state
        model = self.trainer.get_model()

        # load the state_dict on the model automatically
        model.load_state_dict(checkpoint['state_dict'])

        # restore amp scaling
        if self.trainer.amp_backend == AMPType.NATIVE and 'native_amp_scaling_state' in checkpoint:
            self.trainer.scaler.load_state_dict(checkpoint['native_amp_scaling_state'])
        elif self.trainer.amp_backend == AMPType.APEX and 'amp_scaling_state' in checkpoint:
            amp.load_state_dict(checkpoint['amp_scaling_state'])

        if self.trainer.root_gpu is not None:
            model.cuda(self.trainer.root_gpu)

        # load training state (affects trainer only)
        self.restore_training_state(checkpoint)

        # call model hook
        model.on_hpc_load(checkpoint)

        log.info(f'restored hpc model from: {filepath}')

    def max_ckpt_in_folder(self, path, name_key='ckpt_'):
        fs = get_filesystem(path)
        files = [os.path.basename(f) for f in fs.ls(path)]
        files = [x for x in files if name_key in x]
        if len(files) == 0:
            return 0

        ckpt_vs = []
        for name in files:
            name = name.split(name_key)[-1]
            name = re.sub('[^0-9]', '', name)
            ckpt_vs.append(int(name))

        return max(ckpt_vs)

    def save_checkpoint(self, filepath, weights_only: bool = False):
        checkpoint = self.dump_checkpoint(weights_only)

        if self.trainer.is_global_zero:
            # do the actual save
            try:
                atomic_save(checkpoint, filepath)
            except AttributeError as err:
                if LightningModule.CHECKPOINT_HYPER_PARAMS_KEY in checkpoint:
                    del checkpoint[LightningModule.CHECKPOINT_HYPER_PARAMS_KEY]
                rank_zero_warn(
                    'Warning, `module_arguments` dropped from checkpoint.' f' An attribute is not picklable {err}'
                )
                atomic_save(checkpoint, filepath)
