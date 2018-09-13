import argparse
from model_skip import Model
# from model_fc import Model
from os.path import join
from torch.autograd import Variable
from torch.optim import lr_scheduler
import torch.nn.functional as F
from tqdm import tqdm
import torch.distributed as dist
import torch.utils.data.distributed
from DataRead import MuraDataset
import torchvision.transforms as transforms
from sklearn.metrics import classification


def false_loss(GT, pred):
    sub = [GT[i]-pred[i] for i in range(len(GT))]
    add = [GT[i] + pred[i] for i in range(len(GT))]
    FN = float(sub.count(1)) / GT.count(1)
    FP = float(sub.count(-1)) / GT.count(0)
    TN = float(add.count(0)) / GT.count(0)
    TP = float(sub.count(2)) / GT.count(1)
    # prec = float(TP) / (TP+FP)
    # rec = float(TP) / (TP+FN)
    return FN, FP


def loss_fn(x, labels):
    main_loss = F.cross_entropy(x, labels)
    return main_loss


def kappa(y_pred, y_actual, y_pred_totle, y_actual_totle):
    _, pred = torch.topk(y_pred, 1)
    y_actual = y_actual.view(1, -1)[0].cpu().numpy().tolist()
    pred = pred.t().view(1, -1)[0].cpu().numpy().tolist()
    y_pred_totle += pred
    y_actual_totle += y_actual
    return y_pred_totle, y_actual_totle


def accuracy(y_pred, y_actual):
    """Computes the precision@k for the specified values of k"""
    batch_size = y_actual.size(0)

    _, pred = torch.topk(y_pred, 1)
    pred = pred.t().view(1, -1)
    correct = torch.eq(pred, y_actual.view(1, -1)).float().sum(1)/batch_size
    return correct


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CapsNet')
    parser.add_argument('--net', type=str, default='vgg16_skip')
    parser.add_argument('--batch_size', type=int, default=16)
    parser.add_argument('--num_epochs', type=int, default=10)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--dataset', type=str, default='Mura')
    parser.add_argument('--r', type=int, default=3)
    parser.add_argument('--data_csv_dir', default='/data/volume2', metavar='DIR',
                        help='path to dataset csv paths')
    parser.add_argument('--pretrained', type=str, default='')   # /data/volume4/9vgg16ICNN_MURA.pth.tar
    parser.add_argument('--gpu', type=int, default=0, help="which gpu to use")
    parser.add_argument('--num_classes', type=int, default=2, help="how types")
    parser.add_argument('--loss', type=str, default='margin_loss', metavar='N',
                        help='loss to use: cross_entropy_loss, margin_loss, spread_loss')
    parser.add_argument('--routing', type=str, default='angle_routing', metavar='N',
                        help='routing to use: angle_routing, EM_routing, quickshift_routing, '
                             'reduce_noise_angle_routing')
    parser.add_argument('--use-recon', type=bool, default=True, metavar='N',
                        help='use reconstruction loss or not')
    parser.add_argument('--use-additional-loss', type=int, default=0, metavar='B',
                        help='use additional loss: 0: none, 1: contrastive, 2: lifted loss')
    parser.add_argument('--num-workers', type=int, default=4, metavar='N',
                        help='num of workers to fetch data')
    parser.add_argument('-clip', type=float, default=5)
    parser.add_argument('-pai', type=bool, default=True)
    parser.add_argument('--growthRate', type=int, default=12, metavar='N',
                        help='Growth rate for DenseNet.')
    parser.add_argument('--depth', type=int, default=110, help='Model depth.')
    parser.add_argument('--norm_template', type=int, default=1, help='Norm of the template')
    parser.add_argument('--multi-abstract', type=bool, default=False, metavar='N',
                        help='use multi level of abstraction or not')
    parser.add_argument('--dist_backend', default='mpi', type=str, help='distributed backend')

    args = parser.parse_args()
    dist.init_process_group(backend=args.dist_backend)
    print('=>ICNN, using net:', args.net, 'batchsize=', args.batch_size, 'learning_rate=', args.lr,
          'dataset:', args.dataset, 'pre_trained:', args.pretrained)

    data_csv_dir = args.data_csv_dir
    if args.dataset == 'Luna16':
        # Luna16 dataset
        train_csv = join(data_csv_dir, 'train_Luna16_JPG.csv')
        val_csv = join(data_csv_dir, 'valid_Luna16_JPG.csv')
    else:
        # mura dataset
        train_csv = join(data_csv_dir, 'train.csv')
        val_csv = join(data_csv_dir, 'valid.csv')

    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

    # We augment by applying random lateral inversions and rotations.
    train_transforms = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.RandomVerticalFlip(),
        # transforms.RandomRotation(30),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize])

    print('=>load traindata')
    train_dataset = MuraDataset(train_csv, args.dataset, transform=train_transforms)
    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)

    test_dataset = MuraDataset(val_csv, args.dataset, transform=train_transforms)
    test_sampler = torch.utils.data.distributed.DistributedSampler(test_dataset)

    # Data Loader (Input Pipeline)
    train_loader = torch.utils.data.DataLoader(train_dataset,
                                               batch_size=args.batch_size,
                                               num_workers=args.num_workers,
                                               # shuffle=True,
                                               sampler=train_sampler,
                                               pin_memory=True)

    test_loader = torch.utils.data.DataLoader(test_dataset,
                                              batch_size=args.batch_size,
                                              num_workers=args.num_workers,
                                              sampler=test_sampler,
                                              # shuffle=False,
                                              pin_memory=True)

    model = Model(args)
    model = torch.nn.parallel.DistributedDataParallel(model.cuda())
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=2)

    print("# parameters:", sum(param.numel() for param in model.parameters()))
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=2)
    lambda_ = 5e-6

    if args.pretrained:
        print('pretrained:', 'args.pretrained')
        model.load_state_dict(torch.load(args.pretrained))
        m = 0.8
        lambda_ = 0.9

    best_val_loss = 0
    for epoch in range(args.num_epochs):
        # Train
        # print("Epoch {}".format(epoch))
        losses, acc, acc_kappa = 0, 0, 0
        y_pred_totle, y_actual_totle = [], []
        pbar = tqdm(train_loader)
        for i, (imgs, labels, meta) in enumerate(pbar):
            labels = labels.cuda(async=True)
            imgs, labels = Variable(imgs.cuda()), Variable(labels.cuda())
            labels = labels.squeeze()
            out, heatmap, loss_1, loss_2 = model(imgs)
            loss = loss_fn(out, labels) + lambda_ * (loss_1.sum()+loss_2.sum())
            losses += loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            y_pred_totle, y_actual_totle = kappa(out.data, labels.data, y_pred_totle, y_actual_totle)

        pbar.close()
        losses = losses/(i+1)
        acc = torch.eq(torch.Tensor(y_pred_totle), torch.Tensor(y_actual_totle)).sum(0).numpy().tolist()/float(len(y_pred_totle))
        acc_kappa = classification.cohen_kappa_score(y_pred_totle, y_actual_totle)
        FN, FP = false_loss(y_actual_totle, y_pred_totle)
        print("\nEpoch%d Train kappa:%.4f, acc:%.4f, loss:%.4f, FN:%.4f, FP:%.4f" % (epoch, acc_kappa, acc, losses, FN, FP))
        scheduler.step(losses)

        # Test
        # print('Testing...')
        losses, acc, acc_kappa = 0, 0, 0
        y_pred_totle, y_actual_totle = [], []
        model.eval()
        pbar2 = tqdm(test_loader)
        for i, (imgs, labels, meta) in enumerate(pbar2):
            with torch.no_grad():
                labels = labels.cuda(async=True)
                imgs, labels = Variable(imgs.cuda()), Variable(labels.cuda())
                labels = labels.squeeze()
                out, heatmap, loss_1, loss_2 = model(imgs)
                losses += loss_fn(out, labels)
                y_pred_totle, y_actual_totle = kappa(out.data, labels.data, y_pred_totle, y_actual_totle)
        pbar.close()
        losses = losses/(i+1)
        acc = torch.eq(torch.Tensor(y_pred_totle), torch.Tensor(y_actual_totle)).sum(0).numpy().tolist() / float(len(y_pred_totle))
        acc_kappa = classification.cohen_kappa_score(y_pred_totle, y_actual_totle)
        FN, FP = false_loss(y_actual_totle, y_pred_totle)
        rank = torch.distributed.get_rank()
        print("Epoch%d Test prec_kappa:%.4f, acc:%.4f, loss:%.4f, FN:%.4f, FP:%.4f" % (epoch, acc_kappa, acc, losses, FN, FP))
        if rank == 0:
            torch.save(model.state_dict(), '/data/oss_bucket/suqi/train_models/ICNN/mask/vgg16_skip/' + str(epoch) + args.net + 'ICNN_MURA.pth.tar')
        model.train()
