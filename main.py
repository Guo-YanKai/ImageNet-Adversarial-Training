#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: imagenet-resnet-horovod.py

import argparse
import glob
import os
import socket
import numpy as np

import horovod.tensorflow as hvd

from tensorpack import *
from tensorpack.tfutils import get_model_loader

import nets
from adv_model import NoOpAttacker, PGDAttacker
from third_party.imagenet_utils import get_val_dataflow, eval_on_ILSVRC12
from third_party.utils import HorovodClassificationError


def create_eval_callback(name, tower_func, condition):
    """
    Create a distributed evaluation callback.

    Args:
        name (str): a prefix
        tower_func (TowerFuncWrapper): the inference tower function
        condition: a function(epoch number) that returns whether this epoch should evaluate or not
    """
    dataflow = get_val_dataflow(
        args.data, args.batch,
        num_splits=hvd.size(), split_index=hvd.rank())
    # We eval both the classification error rate (for comparison with defensers)
    # and the attack success rate (for comparison with attackers).
    infs = [HorovodClassificationError('wrong-top1', '{}-top1-error'.format(name)),
            HorovodClassificationError('wrong-top5', '{}-top5-error'.format(name)),
            HorovodClassificationError('attack_success', '{}-attack-success-rate'.format(name))
            ]
    cb = InferenceRunner(
        QueueInput(dataflow), infs,
        tower_name=name,
        tower_func=tower_func).set_chief_only(False)
    cb = EnableCallbackIf(
        cb, lambda self: condition(self.epoch_num))
    return cb


def do_train(model):
    batch = args.batch
    total_batch = batch * hvd.size()

    if args.fake:
        data = FakeData(
            [[batch, 224, 224, 3], [batch]], 1000,
            random=False, dtype=['uint8', 'int32'])
        data = StagingInput(QueueInput(data))
        callbacks = []
        steps_per_epoch = 50
    else:
        logger.info("#Tower: {}; Batch size per tower: {}".format(hvd.size(), batch))
        zmq_addr = 'ipc://@imagenet-train-b{}'.format(batch)
        if args.no_zmq_ops:
            dataflow = RemoteDataZMQ(zmq_addr, hwm=150, bind=False)
            data = QueueInput(dataflow)
        else:
            data = ZMQInput(zmq_addr, 30, bind=False)
        data = StagingInput(data, nr_stage=1)

        steps_per_epoch = int(np.round(1281167 / total_batch))

    BASE_LR = 0.1 * (total_batch // 256)
    logger.info("Base LR: {}".format(BASE_LR))
    callbacks = [
        ModelSaver(max_to_keep=10),
        EstimatedTimeLeft(),
        ScheduledHyperParamSetter(
           'learning_rate', [(0, BASE_LR), (35, BASE_LR * 1e-1), (70, BASE_LR * 1e-2),
                             (95, BASE_LR * 1e-3)])
    ]
    """
    Feature Denoising, Sec 5:
    Our models are trained for a total of
    110 epochs; we decrease the learning rate by 10× at the 35-
    th, 70-th, and 95-th epoch
    """
    max_epoch = 110

    if BASE_LR > 0.1:
        callbacks.append(
            ScheduledHyperParamSetter(
                'learning_rate', [(0, 0.1), (5 * steps_per_epoch, BASE_LR)],
                interp='linear', step_based=True))
        """
        ImageNet in 1 Hour, Sec 2.2:
        we start from a learning rate of η and increment it by a constant amount at
        each iteration such that it reaches ηˆ = kη after 5 epochs
        """

    if not args.fake:
        def add_eval_callback(name, attacker, condition):
            cb = create_eval_callback(
                name,
                model.get_inference_func(attacker),
                # always eval in the last 3 epochs no matter what
                lambda epoch_num: condition(epoch_num) or epoch_num > max_epoch - 3)
            callbacks.append(cb)

        add_eval_callback('eval-clean', NoOpAttacker(), lambda e: True)
        add_eval_callback('eval-10step', PGDAttacker(10, args.attack_epsilon, args.attack_step_size),
                          lambda e: True)
        add_eval_callback('eval-50step', PGDAttacker(50, args.attack_epsilon, args.attack_step_size),
                          lambda e: e % 20 == 0)
        add_eval_callback('eval-100step', PGDAttacker(100, args.attack_epsilon, args.attack_step_size),
                          lambda e: e % 10 == 0)
        for k in [20, 30, 40, 60, 70, 80, 90]:
            add_eval_callback('eval-{}step'.format(k),
                              PGDAttacker(k, args.attack_epsilon, args.attack_step_size),
                              lambda e: False)

    trainer = HorovodTrainer(average=True)
    trainer.setup_graph(model.get_inputs_desc(), data, model.build_graph, model.get_optimizer)
    trainer.train_with_defaults(
        callbacks=callbacks,
        steps_per_epoch=steps_per_epoch,
        session_init=get_model_loader(args.load) if args.load is not None else None,
        max_epoch=max_epoch)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--load', help='load model')
    parser.add_argument('--logdir', help='Directory suffix for models and training stats.')
    parser.add_argument('--eval', action='store_true', help='run evaluation with --load instead of training.')
    parser.add_argument('--eval-directory', help='path to a directory of images to classify')

    parser.add_argument('--data', help='ILSVRC dataset dir')
    parser.add_argument('--fake', help='use fakedata to test or benchmark this model', action='store_true')
    parser.add_argument('--no-zmq-ops', help='use pure python to send/receive data',
                        action='store_true')
    parser.add_argument('--batch', help='per-GPU batch size', default=32, type=int)

    # attacker flags:
    parser.add_argument('--attack-iter', help='adversarial attack iteration',
                        type=int, default=30)
    parser.add_argument('--attack-epsilon', help='adversarial attack maximal perturbation',
                        type=float, default=16.0)
    parser.add_argument('--attack-step-size', help='adversarial attack step size',
                        type=float, default=1.0)

    # architecture flags:
    parser.add_argument('-d', '--depth', help='resnet depth',
                        type=int, default=50, choices=[50, 101, 152])
    parser.add_argument('--arch', help='architectures defined in nets.py',
                        default='ResNet')

    args = parser.parse_args()

    # Define model
    model = getattr(nets, args.arch + 'Model')(args)

    # Define attacker
    if args.attack_iter == 0 or args.eval_directory:
        attacker = NoOpAttacker()
    else:
        attacker = PGDAttacker(
            args.attack_iter, args.attack_epsilon, args.attack_step_size,
            prob_start_from_clean=0.2 if not args.eval else 1.0)
    model.set_attacker(attacker)

    os.system("nvidia-smi")
    hvd.init()

    if args.eval:
        sessinit = get_model_loader(args.load)
        if hvd.size() == 1:
            # single-GPU eval, slow
            ds = get_val_dataflow(args.data, args.batch)
            eval_on_ILSVRC12(model, sessinit, ds)
        else:
            cb = create_eval_callback(
                "eval",
                model.get_inference_func(attacker),
                lambda e: True)
            trainer = HorovodTrainer()
            trainer.setup_graph(model.get_inputs_desc(), PlaceholderInput(), model.build_graph, model.get_optimizer)
            # train for an empty epoch, to reuse the distributed evaluation code
            trainer.train_with_defaults(
                callbacks=[cb],
                monitors=[ScalarPrinter()] if hvd.rank() == 0 else [],
                session_init=sessinit,
                steps_per_epoch=0, max_epoch=1)
    if args.eval_directory:
        sessinit = get_model_loader(args.load)
        assert hvd.size() == 1
        files = glob.glob(os.path.join(args.eval_directory, '*.*'))
        ds = ImageFromFile(files, resize=224)
        ds = BatchData(ds, 32, remainder=True)
        ds = MapData(ds, lambda dp: [dp[0][:, :, :, ::-1]])
        # Our model expects BGR images instead of RGB

        pred_config = PredictConfig(
            model=model,
            session_init=sessinit,
            input_names=['input'],
            output_names=['linear/output']  # the logits
        )
        predictor = SimpleDatasetPredictor(pred_config, ds)

        logger.info("Inference on {} images in {}".format(len(files), args.eval_directory))
        results = []
        for logits, in predictor.get_result():
            predictions = list(np.argmax(logits, axis=1))
            results.extend(predictions)
        assert len(results) == len(files)
        output_filename = "predictions.txt"
        with open(output_filename, "w") as f:
            for filename, label in zip(files, results):
                f.write("{}\t{}\n".format(filename, label))
        logger.info("Outputs saved to " + output_filename)
    else:
        logger.info("Training on {}".format(socket.gethostname()))
        args.logdir = os.path.join(
            'train_log',
            'PGD-{}{}-Batch{}-{}GPUs-iter{}-epsilon{}-step{}-{}'.format(
                args.arch, args.depth, args.batch, hvd.size(),
                args.attack_iter, args.attack_epsilon, args.attack_step_size,
                args.logdir))

        if hvd.rank() == 0:
            logger.set_logger_dir(args.logdir, 'd')
        logger.info("Rank={}, Local Rank={}, Size={}".format(hvd.rank(), hvd.local_rank(), hvd.size()))

        do_train(model)
