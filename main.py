#!/usr/bin/env python

import argparse
from time import time
import math

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets

from src import cct as cct_models
from utils.losses import LabelSmoothingCrossEntropy

model_names = sorted(name for name in cct_models.__dict__
                     if name.islower() and not name.startswith("_")
                     and callable(cct_models.__dict__[name]))

best_acc1 = 0


def init_parser():
    parser = argparse.ArgumentParser(description='CIFAR10 quick training script')

    # Data args
    parser.add_argument('data', metavar='DIR',
                        help='path to dataset')

    parser.add_argument('--print-freq', default=10, type=int, metavar='N',
                        help='log frequency (by iteration)')

    # Optimization hyperparams
    parser.add_argument('--epochs', default=200, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('--warmup', default=5, type=int, metavar='N',
                        help='number of warmup epochs')
    parser.add_argument('-b', '--batch-size', default=128, type=int,
                        metavar='N',
                        help='mini-batch size (default: 128)', dest='batch_size')
    parser.add_argument('--lr', default=0.0005, type=float,
                        help='initial learning rate')
    parser.add_argument('--momentum', default=0.9, type=float,
                        help='momentum')
    parser.add_argument('--weight-decay', default=3e-2, type=float,
                        help='weight decay (default: 1e-4)')
    parser.add_argument('--clip-grad-norm', default=0., type=float,
                        help='gradient norm clipping (default: 0 (disabled))')

    parser.add_argument('-m', '--model',
                        type=str.lower,
                        choices=model_names,
                        default='cct_2', dest='model')

    parser.add_argument('-p', '--positional-embedding',
                        type=str.lower,
                        choices=['learnable', 'sine', 'none'],
                        default='learnable', dest='positional_embedding')

    parser.add_argument('--conv-layers', default=2, type=int,
                        help='number of convolutional layers (cct only)')

    parser.add_argument('--conv-size', default=3, type=int,
                        help='convolution kernel size (cct only)')

    parser.add_argument('--patch-size', default=4, type=int,
                        help='image patch size (vit and cvt only)')

    parser.add_argument('--disable-cos', action='store_true',
                        help='disable cosine lr schedule')

    parser.add_argument('--disable-aug', action='store_true',
                        help='disable augmentation policies for training')

    parser.add_argument('--gpu-id', default=0, type=int)

    parser.add_argument('--no-cuda', action='store_true',
                        help='disable cuda')

    return parser


def main():
    global best_acc1

    parser = init_parser()
    args = parser.parse_args()
    n_workers = 8
    img_size = 32
    num_classes = 10
    img_mean, img_std = [0.4914, 0.4822, 0.4465], [0.2470, 0.2435, 0.2616]

    model = cct_models.__dict__[args.model](img_size=img_size,
                                            num_classes=num_classes,
                                            positional_embedding=args.positional_embedding,
                                            n_conv_layers=args.conv_layers,
                                            kernel_size=args.conv_size,
                                            patch_size=args.patch_size)

    print(model)

    criterion = LabelSmoothingCrossEntropy()

    if (not args.no_cuda) and torch.cuda.is_available():
        torch.cuda.set_device(args.gpu_id)
        model.cuda(args.gpu_id)
        criterion = criterion.cuda(args.gpu_id)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    normalize = [transforms.Normalize(mean=img_mean, std=img_std)]

    augmentations = []
    if not args.disable_aug:
        from utils.autoaug import CIFAR10Policy
        augmentations += [
            CIFAR10Policy()
        ]
    augmentations += [
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        *normalize,
    ]

    augmentations = transforms.Compose(augmentations)
    train_dataset = datasets.CIFAR10(root=args.data, train=True, download=True,
                                     transform=augmentations)

    val_dataset = datasets.CIFAR10(
        root=args.data, train=False, download=False, transform=transforms.Compose([
            transforms.Resize(img_size),
            transforms.ToTensor(),
            *normalize,
        ]))

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=True,
        num_workers=n_workers)

    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=args.batch_size, shuffle=False,
        num_workers=n_workers)

    time_begin = time()
    for epoch in range(args.epochs):
        adjust_learning_rate(optimizer, epoch, args)
        cls_train(train_loader, model, criterion, optimizer, epoch, args)
        acc1 = cls_validate(val_loader, model, criterion, args, epoch=epoch, time_begin=time_begin)
        best_acc1 = max(acc1, best_acc1)

    total_mins = (time() - time_begin) / 60
    print(f'Script finished in {total_mins} minutes, best top-1: {best_acc1}, final top-1: {acc1}')


def adjust_learning_rate(optimizer, epoch, args):
    lr = args.lr
    if hasattr(args, 'warmup') and epoch < args.warmup:
        lr = lr / (args.warmup - epoch)
    elif not args.disable_cos:  # cosine lr schedule
        lr *= 0.5 * (1. + math.cos(math.pi * (epoch - args.warmup) / (args.epochs - args.warmup)))

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def accuracy(output, target):
    with torch.no_grad():
        batch_size = target.size(0)

        _, pred = output.topk(1, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        correct_k = correct[:1].flatten().float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
        return res


def cls_train(train_loader, model, criterion, optimizer, epoch, args):
    model.train()
    loss_val, acc1_val = 0, 0
    n = 0
    for i, (images, target) in enumerate(train_loader):
        if (not args.no_cuda) and torch.cuda.is_available():
            images = images.cuda(args.gpu_id, non_blocking=True)
            target = target.cuda(args.gpu_id, non_blocking=True)
        output = model(images)

        loss = criterion(output, target)

        acc1 = accuracy(output, target)
        n += images.size(0)
        loss_val += float(loss.item() * images.size(0))
        acc1_val += float(acc1[0] * images.size(0))

        optimizer.zero_grad()
        loss.backward()

        if args.clip_grad_norm > 0:
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=args.clip_grad_norm, norm_type=2)

        optimizer.step()

        if args.print_freq is not None and i % args.print_freq == 0:
            avg_loss, avg_acc1 = (loss_val / n), (acc1_val / n)
            print('[Epoch {}][Train][{}] \t Loss: {:.4e} \t Top-1 {:6.2f}'.format(epoch,
                                                                                  i,
                                                                                  avg_loss,
                                                                                  avg_acc1))


def cls_validate(val_loader, model, criterion, args, epoch=None, time_begin=None):
    model.eval()
    loss_val, acc1_val = 0, 0
    n = 0
    with torch.no_grad():
        for i, (images, target) in enumerate(val_loader):
            if (not args.no_cuda) and torch.cuda.is_available():
                images = images.cuda(args.gpu_id, non_blocking=True)
                target = target.cuda(args.gpu_id, non_blocking=True)

            output = model(images)
            loss = criterion(output, target)

            acc1 = accuracy(output, target)
            n += images.size(0)
            loss_val += float(loss.item() * images.size(0))
            acc1_val += float(acc1[0] * images.size(0))

            if args.print_freq is not None and i % args.print_freq == 0:
                avg_loss, avg_acc1 = (loss_val / n), (acc1_val / n)
                print('[Epoch {}][Eval][{}] \t Loss: {:.4e} \t Top-1 {:6.2f}'.format(epoch,
                                                                                     i,
                                                                                     avg_loss,
                                                                                     avg_acc1))

    avg_loss, avg_acc1 = (loss_val / n), (acc1_val / n)
    mins_since_beg = -1 if time_begin is None else (time() - time_begin) / 60
    print('[Epoch {}] \t \t Top-1 {:6.2f} \t Time: {:.2f}'.format(epoch, avg_acc1, mins_since_beg))

    return avg_acc1


if __name__ == '__main__':
    main()
