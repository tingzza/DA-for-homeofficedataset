import argparse
import os
import os.path as osp

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import network
import loss
import pre_process as prep
import torch.utils.data as util_data
import lr_schedule
import data_list
from data_list import ImageList
from torch.autograd import Variable
import random
import matplotlib.pyplot as plt

optim_dict = {"SGD": optim.SGD}


def image_classification_test(loader, model, test_10crop=True, gpu=True, iter_num=-1):
    start_test = True
    if test_10crop:
        iter_test = [iter(loader['test' + str(i)]) for i in range(10)]
        for i in range(len(loader['test0'])):
            data = [iter_test[j].__next__() for j in range(10)]
            inputs = [data[j][0] for j in range(10)]
            labels = data[0][1]
            if gpu:
                for j in range(10):
                    inputs[j] = Variable(inputs[j].cuda())
                labels = Variable(labels.cuda())
            else:
                for j in range(10):
                    inputs[j] = Variable(inputs[j])
                labels = Variable(labels)
            outputs = []
            for j in range(10):
                _, predict_out = model(inputs[j])
                outputs.append(nn.Softmax(dim=1)(predict_out))
            outputs = sum(outputs)
            if start_test:
                all_output = outputs.data.float()
                all_label = labels.data.float()
                start_test = False
            else:
                all_output = torch.cat((all_output, outputs.data.float()), 0)
                all_label = torch.cat((all_label, labels.data.float()), 0)
    else:
        iter_test = iter(loader["test"])
        for i in range(len(loader['test'])):
            data = iter_test.__next__()
            inputs = data[0]
            labels = data[1]
            if gpu:
                inputs = Variable(inputs.cuda())
                labels = Variable(labels.cuda())
            else:
                inputs = Variable(inputs)
                labels = Variable(labels)
            _, outputs = model(inputs)
            if start_test:
                all_output = outputs.data.float()
                all_label = labels.data.float()
                start_test = False
            else:
                all_output = torch.cat((all_output, outputs.data.float()), 0)
                all_label = torch.cat((all_label, labels.data.float()), 0)
    _, predict = torch.max(all_output, 1)
    accuracy = torch.sum(torch.squeeze(predict).float() == all_label) / float(all_label.size()[0])
    return accuracy


def train(config):
    lossdecay=[]
    trainacc=[]
    testacc=[]
    ## set pre-process
    prep_dict = {}
    prep_config = config["prep"]
    prep_dict["source"] = prep.image_train( \
        resize_size=prep_config["resize_size"], \
        crop_size=prep_config["crop_size"])
    prep_dict["target"] = prep.image_train( \
        resize_size=prep_config["resize_size"], \
        crop_size=prep_config["crop_size"])
    if prep_config["test_10crop"]:
        prep_dict["test"] = prep.image_test_10crop( \
            resize_size=prep_config["resize_size"], \
            crop_size=prep_config["crop_size"])
    else:
        prep_dict["test"] = prep.image_test( \
            resize_size=prep_config["resize_size"], \
            crop_size=prep_config["crop_size"])

    ## set loss
    class_criterion = nn.CrossEntropyLoss()
    transfer_criterion = loss.DANN
    loss_params = config["loss"]

    ## prepare data
    dsets = {}
    dset_loaders = {}
    data_config = config["data"]
    dsets["source"] = ImageList(open(data_config["source"]["list_path"]).readlines(), \
                                transform=prep_dict["source"])
    dset_loaders["source"] = util_data.DataLoader(dsets["source"], \
                                                  batch_size=data_config["source"]["batch_size"], \
                                                  shuffle=True, num_workers=2)
    dsets["target"] = ImageList(open(data_config["target"]["list_path"]).readlines(), \
                                transform=prep_dict["target"])
    dset_loaders["target"] = util_data.DataLoader(dsets["target"], \
                                                  batch_size=data_config["target"]["batch_size"], \
                                                  shuffle=True, num_workers=2)

    if prep_config["test_10crop"]:
        for i in range(10):
            dsets["test" + str(i)] = ImageList(open(data_config["test"]["list_path"]).readlines(), \
                                               transform=prep_dict["test"]["val" + str(i)])
            dset_loaders["test" + str(i)] = util_data.DataLoader(dsets["test" + str(i)], \
                                                                 batch_size=data_config["test"]["batch_size"], \
                                                                 shuffle=False, num_workers=2)

            dsets["target" + str(i)] = ImageList(open(data_config["target"]["list_path"]).readlines(), \
                                                 transform=prep_dict["test"]["val" + str(i)])
            dset_loaders["target" + str(i)] = util_data.DataLoader(dsets["target" + str(i)], \
                                                                   batch_size=data_config["test"]["batch_size"], \
                                                                   shuffle=False, num_workers=2)
    else:
        dsets["test"] = ImageList(open(data_config["test"]["list_path"]).readlines(), \
                                  transform=prep_dict["test"])
        dset_loaders["test"] = util_data.DataLoader(dsets["test"], \
                                                    batch_size=data_config["test"]["batch_size"], \
                                                    shuffle=False, num_workers=2)

        dsets["target_test"] = ImageList(open(data_config["target"]["list_path"]).readlines(), \
                                         transform=prep_dict["test"])
        dset_loaders["target_test"] = util_data.DataLoader(dsets["target_test"], \
                                                   batch_size=data_config["test"]["batch_size"], \
                                                   shuffle=False, num_workers=2)

    class_num = config["network"]["params"]["class_num"]

    ## set base network 模型初始化啦
    net_config = config["network"]
    base_network = net_config["name"](**net_config["params"])

    use_gpu = torch.cuda.is_available()
    if use_gpu:
        base_network = base_network.cuda()

    ## collect parameters
    if net_config["params"]["new_cls"]:
        if net_config["params"]["use_bottleneck"]:
            parameter_list = [{"params": base_network.feature_layers.parameters(), "lr": 1}, \
                              {"params": base_network.bottleneck.parameters(), "lr": 10}, \
                              {"params": base_network.fc.parameters(), "lr": 10}]
        else:
            parameter_list = [{"params": base_network.feature_layers.parameters(), "lr": 1}, \
                              {"params": base_network.fc.parameters(), "lr": 10}]
    else:
        parameter_list = [{"params": base_network.parameters(), "lr": 1}]

    ## add additional network for some methods
    class_weight = torch.from_numpy(np.array([1.0] * class_num))
    if use_gpu:
        class_weight = class_weight.cuda()
    ad_net = network.AdversarialNetwork(base_network.output_num())
    gradient_reverse_layer = network.GradientReverseLayer(high_value=config["high"])
    if use_gpu:
        ad_net = ad_net.cuda()
    parameter_list.append({"params": ad_net.parameters(), "lr": 10})

    ## set optimizer 优化器初始化。在训练开始前初始化完成
    optimizer_config = config["optimizer"]
    optimizer = optim_dict[optimizer_config["type"]](parameter_list, \
                                                     **(optimizer_config["optim_params"]))
    param_lr = []
    for param_group in optimizer.param_groups:
        param_lr.append(param_group["lr"])
    schedule_param = optimizer_config["lr_param"]
    lr_scheduler = lr_schedule.schedule_dict[optimizer_config["lr_type"]]

    ## train开始训练！
    len_train_source = len(dset_loaders["source"]) - 1#计算一个epoch里面batch的大小
    len_train_target = len(dset_loaders["target"]) - 1#计算一个epoch里面batch的大小
    transfer_loss_value = classifier_loss_value = total_loss_value = 0.0
    best_acc = 0.0
    for i in range(config["num_iterations"]):
        if i % config["test_interval"] == 0:
            base_network.train(False)
            temp_acc = image_classification_test(dset_loaders, \
                                                 base_network, test_10crop=prep_config["test_10crop"], \
                                                 gpu=use_gpu)
            temp_model = nn.Sequential(base_network)
            if temp_acc > best_acc:
                best_acc = temp_acc
                best_model = temp_model

            testacc.append(temp_acc.item())

            log_str = "iter: {:05d}, precision: {:.5f}".format(i, temp_acc)
            config["out_file"].write(log_str)
            config["out_file"].flush()
            print(log_str)#每50次batch打印一下准确率
        if i % config["snapshot_interval"] == 0:#每125次，大约一个epoch保存一次模型
            torch.save(nn.Sequential(base_network), osp.join(config["output_path"], \
                                                             "iter_{:05d}_model.pth.tar".format(i)))

        ## train one iter
        base_network.train(True)
        optimizer = lr_scheduler(param_lr, optimizer, i, **schedule_param)
        optimizer.zero_grad()
        if i % len_train_source == 0:#rain_source的一个epoch结束
            iter_source = iter(dset_loaders["source"])#导入下一个打乱顺序的sourceepoch
        if i % len_train_target == 0:#train_target的一个epoch结束
            iter_target = iter(dset_loaders["target"])#导入下一个打乱顺序的targetepoch
        inputs_source, labels_source = iter_source.__next__()
        inputs_target, labels_target = iter_target.__next__()
        if use_gpu:
            inputs_source, inputs_target, labels_source = \
                Variable(inputs_source).cuda(), Variable(inputs_target).cuda(), \
                Variable(labels_source).cuda()
        else:
            inputs_source, inputs_target, labels_source = Variable(inputs_source), \
                                                          Variable(inputs_target), Variable(labels_source)

        inputs = torch.cat((inputs_source, inputs_target), dim=0)#source和target数量相同，拼接在一起
        features, outputs = base_network(inputs)#一个batch

        softmax_out = nn.Softmax(dim=1)(outputs).detach()  ##provide a papr for entropy
        ad_net.train(True)
        #训练损失计算
        transfer_loss = transfer_criterion(features, ad_net, gradient_reverse_layer, use_gpu)
        classifier_loss = class_criterion(outputs.narrow(0, 0, int(inputs.size(0) / 2)), labels_source)
        total_loss = loss_params["trade_off"] * transfer_loss + classifier_loss
        lossdecay.append(total_loss.item())#保存每一个batch的loss
        #梯度计算与参数更新
        total_loss.backward()
        optimizer.step()
        #训练准确率计算
        _, predicted_classes = torch.max(outputs.narrow(0, 0, int(inputs.size(0) / 2)), dim=1)  # shape: [half_size]
        correct = (predicted_classes == labels_source).sum().item()
        accuracy = correct / labels_source.size(0)
        trainacc.append(accuracy)#保存每一个batch的准确率

    torch.save(best_model, osp.join(config["output_path"], "best_model.pth.tar"))

    # 绘制损失和准确率曲线
    plt.figure(figsize=(12, 4))
    plt.subplot(1, 2, 1)
    plt.plot(lossdecay, label='Train Loss')
    plt.xlabel('Batches')
    plt.ylabel('Loss')
    plt.legend()
    
    plt.subplot(1, 2, 2)
    plt.plot(range(config["num_iterations"]),trainacc, label='Train Acc')
    plt.plot(range(0, config["num_iterations"],config["test_interval"]),testacc, label='Test Acc')
    plt.xlabel('Batches')
    plt.ylabel('Accuracy')
    plt.legend()

    plt.savefig(osp.join(config["output_path"],"linecharts.png"), dpi=300, bbox_inches='tight')  # dpi 提高清晰度
    #plt.show()

    return best_acc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Transfer Learning')
    parser.add_argument('--gpu_id', type=str, nargs='?', default='0', help="device id to run")
    parser.add_argument('--net', type=str, default='ResNet50', help="Options: ResNet18,34,50,101,152; AlexNet")
    parser.add_argument('--dset', type=str, default='office-home', help="The dataset or source dataset used")
    parser.add_argument('--s_dset_path', type=str, default='../data/office-home/Product.txt',
                        help="The source dataset path list")
    parser.add_argument('--t_dset_path', type=str, default='../data/office-home/Clipart.txt',
                        help="The target dataset path list")
    parser.add_argument('--test_interval', type=int, default=125, help="interval of two continuous test phase")
    parser.add_argument('--snapshot_interval', type=int, default=375, help="interval of two continuous output model")
    parser.add_argument('--output_dir', type=str, default='dann',
                        help="output directory of our model (in ../snapshot directory)")
    args = parser.parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_id

    # train config
    config = {}
    config["softmax_param"] = 1.0
    config["high"] = 1.0
    config["num_iterations"] = 1251
    config["test_interval"] = args.test_interval
    config["snapshot_interval"] = args.snapshot_interval
    config["output_for_test"] = True
    config["output_path"] = "../snapshot/" + args.output_dir
    if not osp.exists(config["output_path"]):
        os.mkdir(config["output_path"])
    config["out_file"] = open(osp.join(config["output_path"], "log.txt"), "w")#写入模式,全新写入
    config["prep"] = {"test_10crop": True, "resize_size": 256, "crop_size": 224}
    config["loss"] = {"trade_off": 1.0, "update_iter": 500}
    #网络选择
    if "AlexNet" in args.net:
        config["network"] = {"name": network.AlexNetFc, \
                             "params": {"use_bottleneck": True, "bottleneck_dim": 256, "new_cls": True}}
    elif "ResNet" in args.net:
        config["network"] = {"name": network.ResNetFc, \
                             "params": {"resnet_name": args.net, "use_bottleneck": True, "bottleneck_dim": 256,
                                        "new_cls": True}}
    elif "VGG" in args.net:
        config["network"] = {"name": network.VGGFc, \
                             "params": {"vgg_name": args.net, "use_bottleneck": True, "bottleneck_dim": 256,
                                        "new_cls": True}}
    config["optimizer"] = {"type": "SGD", "optim_params": {"lr": 1.0, "momentum": 0.9, \
                                                           "weight_decay": 0.0005, "nesterov": True}, "lr_type": "inv", \
                           "lr_param": {"init_lr": 0.001, "gamma": 0.001, "power": 0.75}}
    #数据集选择
    config["dataset"] = args.dset
    if config["dataset"] == "office":
        config["data"] = {"source": {"list_path": args.s_dset_path, "batch_size": 36}, \
                          "target": {"list_path": args.t_dset_path, "batch_size": 36}, \
                          "test": {"list_path": args.t_dset_path, "batch_size": 4}}
        if "amazon" in config["data"]["test"]["list_path"]:
            config["optimizer"]["lr_param"]["init_lr"] = 0.0003
        else:
            config["optimizer"]["lr_param"]["init_lr"] = 0.001
        config["loss"]["update_iter"] = 500
        config["network"]["params"]["class_num"] = 31
    elif config["dataset"] == "office-home":#这是我使用的数据集
        config["data"] = {"source": {"list_path": args.s_dset_path, "batch_size": 36}, \
                          "target": {"list_path": args.t_dset_path, "batch_size": 36}, \
                          "test": {"list_path": args.t_dset_path, "batch_size": 4}}
        if "Real_World" in args.s_dset_path and "Art" in args.t_dset_path:
            config["softmax_param"] = 1.0
            config["optimizer"]["lr_param"]["init_lr"] = 0.0003
        elif "Real_World" in args.s_dset_path:
            config["softmax_param"] = 10.0
            config["optimizer"]["lr_param"]["init_lr"] = 0.001
        elif "Art" in args.s_dset_path:
            config["optimizer"]["lr_param"]["init_lr"] = 0.0003
            config["high"] = 0.5
            config["softmax_param"] = 10.0
            if "Real_World" in args.t_dset_path:
                config["high"] = 0.25
        elif "Product" in args.s_dset_path:
            config["optimizer"]["lr_param"]["init_lr"] = 0.0003
            config["high"] = 0.5
            config["softmax_param"] = 10.0
            if "Real_World" in args.t_dset_path:
                config["high"] = 0.3
        else:
            config["optimizer"]["lr_param"]["init_lr"] = 0.0003
            if "Real_World" in args.t_dset_path:
                config["high"] = 0.5
                config["softmax_param"] = 10.0
                config["loss"]["update_iter"] = 1000
            else:
                config["high"] = 0.5
                config["softmax_param"] = 10.0
                config["loss"]["update_iter"] = 500
        config["network"]["params"]["class_num"] = 65
    elif config["dataset"] == "imagenet":
        config["data"] = {"source": {"list_path": args.s_dset_path, "batch_size": 36}, \
                          "target": {"list_path": args.t_dset_path, "batch_size": 36}, \
                          "test": {"list_path": args.t_dset_path, "batch_size": 4}}
        config["optimizer"]["lr_param"]["init_lr"] = 0.0003
        config["loss"]["update_iter"] = 2000
        config["network"]["params"]["use_bottleneck"] = False
        config["network"]["params"]["new_cls"] = False
        config["network"]["params"]["class_num"] = 1000
    elif config["dataset"] == "caltech":
        config["data"] = {"source": {"list_path": args.s_dset_path, "batch_size": 36}, \
                          "target": {"list_path": args.t_dset_path, "batch_size": 36}, \
                          "test": {"list_path": args.t_dset_path, "batch_size": 4}}
        config["optimizer"]["lr_param"]["init_lr"] = 0.001
        config["loss"]["update_iter"] = 500
        config["network"]["params"]["class_num"] = 256
    besttestacc=train(config)
    print("best test acc:",besttestacc)
    print("Done!")