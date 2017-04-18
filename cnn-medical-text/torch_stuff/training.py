"""
    Train a model with PyTorch
"""
import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.autograd as autograd
from torch.autograd import Variable

from constants import *
import datasets
import evaluation
import persistence
import torch_stuff.models as models
import torch_stuff.objectives as objectives

import csv
import argparse
import os
import numpy as np
import random
import sys
import time

def main(Y, vocab_min, model_name, n_epochs, objective, norm_constraint, filter_size,
         min_filter, max_filter, num_filter_maps, lstm_dim, saved_dir, data_path, gpu, split_batch, stochastic):
    """
        main function which sequentially loads the data, builds the model, trains, evaluates, writes output, etc.
    """
    #need to handle really large text fields
    csv.field_size_limit(sys.maxsize)
    if model_name == "cnn_multi":
        model = models.MultiConv(Y, min_filter, max_filter, num_filter_maps, norm_constraint)
        if gpu:
            model.cuda()
    elif model_name == "lstm":
        model = models.VanillaLSTM(Y, lstm_dim, gpu)
        if gpu:
            model.cuda()
    elif model_name == "cnn_vanilla":
        model = models.VanillaConv(Y, filter_size, num_filter_maps)
        if gpu:
            model.cuda()
    elif model_name == "mlp":
        model = models.MLP(Y)
        if gpu:
            model.cuda()
    elif model_name == "logreg":
        model = models.LogReg(Y)
        if gpu:
            model.cuda()
    elif model_name == "saved":
        model, filter_size, min_filter, max_filter, num_filter_maps, prev_epochs, prev_metrics, prev_dataset\
            = persistence.load_model(saved_dir, Y, vocab_min)
        sys.exit(0)

    #optimizer = optim.SGD(model.parameters(), lr=LEARNING_RATE)
    #optimizer = optim.RMSprop(model.parameters())
    #optimizer = optim.Adadelta(model.parameters(), rho=0.95)
    optimizer = optim.Adam(model.parameters())
    #optimizer = optim.Adagrad(model.parameters(), lr=.01)

    model_dir = os.path.join(MODEL_DIR, '_'.join([model_name, time.strftime('%b_%d_%H:%M', time.gmtime())]))

    #load vocab
    v_dict, c_dict = datasets.load_lookups("full", Y, vocab_min)
    desc_dict = datasets.load_code_descriptions()
    dicts = (v_dict, c_dict, desc_dict)

    min_size = max_filter if model_name == "cnn_multi" else filter_size
    names = ["acc", "prec", "rec", "f1", "auc"]
    names.extend(["%s_micro" % (name) for name in names])
    metrics_hist = {name: [] for name in names}
    metrics_hist_tr = {name: [] for name in names}
    metrics_hist["loss"] = []
    if stochastic:
        insts = load_insts(Y, "full", BATCH_SIZE)
    for epoch in range(n_epochs):
        start = time.time()
        if stochastic:
            loss = train_stochastic(model, insts, optimizer, Y, epoch, data_path, min_size, gpu, objective)
        else:
            loss = train(model, optimizer, Y, epoch, data_path, min_size, gpu, split_batch, objective)
        end = time.time()
        print("train time: " + str(end - start) + " s")
        print("epoch loss: " + str(loss))
        metrics_hist["loss"].append(loss)
        print("evaluating on dev")
        start = time.time()
        metrics, fpr, tpr = test(model, Y, epoch, data_path, "dev", min_size, gpu, split_batch, dicts=dicts)
        end = time.time()
        print("test time: " + str(end - start))
        for name in names:
            metrics_hist[name].append(metrics[name])

        print("sanity check on train")
        metrics_t, _, _ = test(model, Y, epoch, data_path, "train", min_size, gpu, split_batch, print_samples=False)
        for name in names:
            metrics_hist_tr[name].append(metrics_t[name])
        #save metric history, model, params
        if model_name != "saved": 
            if epoch == 0:
                os.mkdir(model_dir)
            persistence.save_metrics(metrics_hist, metrics_hist_tr, model_dir)
            persistence.save_params(model_dir, Y, vocab_min, data_path, model_name, n_epochs, "torch", filter_size, 
                                    min_filter, max_filter, num_filter_maps)
            torch.save(model, model_dir + "/model.pth")

    if model == "saved" and data_path != prev_dataset:
        response = raw_input("***WARNING*** you ran the saved model on a different dataset than it was previously trained on. Overwrite? (y/n) > ")
        if "y" not in response:
            print("not saving any results (model, params, or metrics)")
            sys.exit(0)
        dataset = ",".join([prev_dataset, dataset])
        #overwrite old files w/ new values, rename folder
        persistence.rewrite_metrics(prev_metrics, metrics_hist, metrics_hist_tr, saved_dir)
        persistence.rewrite_params(saved_dir, dataset, prev_epochs+n_epochs)
        torch.save(model, saved_dir + "/model.h5")


def train(model, optimizer, Y, epoch, data_path, min_size, gpu, split_batch, objective):
    filename = DATA_DIR + "/notes_" + str(Y) + "_train.csv" if data_path is None else data_path
    #put model in "train" mode
    model.train()
    losses = []
    if objective == "warp":
        batch_size = 1
        print_every = 5000
    else:
        batch_size = BATCH_SIZE
        print_every = 10
    start_inds = None
    if split_batch:
        gen = datasets.split_docs_generator(filename, batch_size, BATCH_LENGTH, min_size, Y)
    else:
        gen = datasets.data_generator(filename, batch_size, Y)
    for batch_idx, tup in enumerate(gen):
        if split_batch:
            data, target, start_inds = tup
        else:
            data, target = tup
        data, target = Variable(torch.LongTensor(data)), Variable(torch.FloatTensor(target))
        if data.size()[1] < min_size:
            continue
        if gpu:
            #gpu-ify
            data = data.cuda()
            target = target.cuda()
        #clear gradients
        optimizer.zero_grad()
        #model.zero_grad()
        #forward computation
        if isinstance(model, models.VanillaLSTM):
            model.refresh(data.size()[0])
        output = model(data, start_inds)
        if objective == "warp":
            output = output.squeeze()
            target = target.squeeze()
            loss = objectives.warp_loss(output, target)
            if loss.size()[0] > 1:
                loss = loss.sum()
                loss.backward()
                optimizer.step()
        else:
            output = F.sigmoid(output)
            loss = F.binary_cross_entropy(output, target)
            #backward pass
            loss.backward()
            optimizer.step()
#            if batch_idx % 100 == 0:
#                print(model.fc.weight.data.sum())
        losses.append(loss.data[0])
        model.enforce_norm_constraint()
        if batch_idx % print_every == 0:
            print("Train epoch: {} [batch #{}, batch_size {}, seq length {}]\tLoss: {:.6f}".format(
                epoch+1, batch_idx, data.size()[0], data.size()[1], np.mean(losses)))
    return np.mean(losses)

def load_insts(Y, data_path, batch_size):
    filename = DATA_DIR + "/notes_" + str(Y) + "_train.csv" if data_path is None else data_path
    print("loading all instances...")
    insts = []
    for (data, target) in datasets.data_generator(filename, batch_size, Y):
        insts.append((data, target))
    return insts 

def train_stochastic(model, insts, optimizer, Y, epoch, dataset, min_size, gpu, objective):
    #put model in "train" mode
    model.train()
    losses = []
    if objective == "warp":
        print_every = 5000
    else:
        print_every = 50
    np.random.shuffle(insts)
    for batch_idx, (data, target) in enumerate(insts):
        data, target = Variable(torch.LongTensor(data)), Variable(torch.FloatTensor(target))
        #make em 2d
#        data = data.unsqueeze(0)
#        target = target.unsqueeze(0)
        if data.size()[1] < min_size:
            continue
        if gpu:
            #gpu-ify
            data = data.cuda()
            target = target.cuda()
        #clear gradients
        optimizer.zero_grad()
        #model.zero_grad()
        #forward computation
        output = model(data)
        if objective == "warp":
            output = output.squeeze()
            target = target.squeeze()
            loss = objectives.warp_loss(output, target)
            if loss.size()[0] > 1:
                loss = loss.sum()
                loss.backward()
                optimizer.step()
        else:
            output = F.sigmoid(output)
            loss = F.binary_cross_entropy(output, target)
            #backward pass
            loss.backward()
            optimizer.step()
        losses.append(loss.data[0])
        model.enforce_norm_constraint()
        if batch_idx % print_every == 0:
            print("Train epoch: {} [inst #{}, batch_size {}, seq length {}]\tLoss: {:.6f}".format(
                epoch+1, batch_idx, data.size()[0], data.size()[1], np.mean(losses)))
    return np.mean(losses)

def test(model, Y, epoch, data_path, fold, min_size, gpu, split_batch, dicts=None, print_samples=True):
    filename = DATA_DIR + "/notes_" + str(Y) + "_" + fold + ".csv" if data_path is None else data_path.replace("train", fold)
    #put model in "test" mode
    model.eval()
    y = []
    yhat = []
    yhat_raw = []
    start_inds = None
    if split_batch:
        gen = datasets.split_docs_generator(filename, BATCH_SIZE, BATCH_LENGTH, min_size, Y)
    else:
        gen = datasets.data_generator(filename, BATCH_SIZE, Y)
    for batch_idx, tup in enumerate(gen):
        if split_batch:
            data, target, start_inds = tup
        else:
            data, target = tup
        data, target = Variable(torch.LongTensor(data), volatile=True), Variable(torch.FloatTensor(target))
        if data.size()[1] < min_size:
            continue
        if gpu:
            #gpu-ify
            data = data.cuda()
            target = target.cuda()
        #clear gradients
        model.zero_grad()
        #predict
        output = F.sigmoid(model(data, start_inds))
        output = output.data.cpu().numpy()
        target_data = target.data.cpu().numpy()

        if np.random.rand() > 0.999 and print_samples:
            print("sample prediction")
            print("Y_true: " + str(target_data[0]))
            print("Y_hat: " + str(output[0]))
            output = np.round(output)
            print("Y_hat: " + str(output[0]))
            print
        if dicts is not None:
            v_dict, c_dict, desc_dict = dicts
            if margin_worse_than(-0.5, output[0], target_data[0]):
                print("did bad on this one")
                print("Y_true: " + str(target_data[0]))
                print("Y_hat: " + str(output[0]))
                print("first 100 words:")
                words = [v_dict[w] for w in data.data[0,:]]
                print(" ".join(words[:100]))
                codes = [str(c_dict[code]) for code in np.where(target_data[0] == 1)[0]]
                print("codes / descriptions")
                print(", ".join([code + ": " + desc_dict[code] for code in codes]))
                print
            if margin_better_than(0.5, output[0], target_data[0]):
                print("did good on this one")
                print("Y_true: " + str(target_data[0]))
                print("Y_hat: " + str(output[0]))
                print("first 100 words:")
                words = [v_dict[w] for w in data.data[0,:]]
                print(" ".join(words[:100]))
                codes = [str(c_dict[code]) for code in np.where(target_data[0] == 1)[0]]
                print("codes / descriptions")
                print(", ".join([code + ": " + desc_dict[code] for code in codes]))
                print

        output = np.round(output)
        y.append(target_data)
        yhat.append(output)

    y = np.concatenate(y, axis=0)
    yhat = np.concatenate(yhat, axis=0)
    metrics, fpr, tpr = evaluation.all_metrics(yhat, y)
    if epoch % 1 == 0:
        evaluation.print_metrics(metrics)
    return metrics, fpr, tpr

def margin_worse_than(margin, output, target):
    min_true = sys.maxint
    max_false = -1*sys.maxint
    for i in range(len(target)):
        if target[i] == 1 and output[i] < min_true:
            min_true = output[i]
        elif target[i] == 0 and output[i] > max_false:
            max_false = output[i]
    return (min_true - max_false) < margin 

def margin_better_than(margin, output, target):
    min_true = sys.maxint
    max_false = -1*sys.maxint
    for i in range(len(target)):
        if target[i] == 1 and output[i] < min_true:
            min_true = output[i]
        elif target[i] == 0 and output[i] > max_false:
            max_false = output[i]
    return (min_true - max_false) > margin

def check_args(args):
    if args.model == "saved" and args.saved_dir is None:
        return False, "Specified 'saved' but no model path given"
    if args.model == "lstm" and args.lstm_dim is None:
        return False, "Specified 'lstm' but no lstm dim given"
    if args.model == "cnn_vanilla" and args.filter_size is None:
        return False, "Specified 'cnn_vanilla' but no filter size given"
    if args.model == "cnn_multi" and (args.min_filter is None or args.max_filter is None):
        return False, "Specified 'cnn_multi', but (min_filter, max_filter) not fully specified"
    if (args.model == "cnn_vanilla" or args.model == "cnn_multi") and args.num_filter_maps is None:
        return False, "Specified a cnn model but no num_filter_maps given"
    else:
        return True, "OK"

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("Y", type=int, help="size of label space")
    parser.add_argument("vocab_min", type=int, help="vocab parameter (min # occurrences)")
    parser.add_argument("model", type=str, choices=["cnn_vanilla", "cnn_multi", "lstm", "saved", "mlp", "logreg"], 
                        help="model")
    parser.add_argument("n_epochs", type=int, help="number of epochs to train")
    parser.add_argument("objective", type=str, choices=["warp", "bce"], help="which objective")
    parser.add_argument("norm_constraint", type=int, help="l2 norm of weight vectors should be less than this value")
    parser.add_argument("--lstm-dim", type=int, required=False, dest="lstm_dim",
                        help="size of lstm hidden layer")
    parser.add_argument("--filter-size", type=int, required=False, dest="filter_size",
                        help="size of convolution filter to use (cnn_vanilla only)")
    parser.add_argument("--min-filter", type=int, required=False, dest="min_filter",
                        help="min size of filter range to use (cnn_multi only)")
    parser.add_argument("--max-filter", type=int, required=False, dest="max_filter",
                        help="max size of filter range to use (cnn_multi only)")
    parser.add_argument("--num-filter-maps", type=int, required=False, dest="num_filter_maps",
                        help="size of conv output")
    parser.add_argument("--saved-model", type=str, required=False, dest="saved_dir",
                        help="path to a directory containing a saved model (and params and metrics) to load instead of building one")
    parser.add_argument("--data-path", type=str, required=False, dest="data_path",
                        help="optional path to a file containing sorted data. will go to DATA_DIR/notes_Y_train.csv by default")
    parser.add_argument("--gpu", dest="gpu", action="store_const", required=False, const=True,
                        help="optional flag to use GPU if available")
    parser.add_argument("--split-batch", dest="split_batch", action="store_const", required=False, const=True,
                        help="optional flag to use the new batching method that splits instances")
    parser.add_argument("--stochastic", dest="stochastic", action="store_const", required=False, const=True,
                        help="optional flag to randomly sample data instead of running through it sequentially")
    args = parser.parse_args()
    ok, msg = check_args(args)
    if ok:
        main(args.Y, args.vocab_min, args.model, args.n_epochs, args.objective, args.norm_constraint, args.filter_size,
                args.min_filter, args.max_filter, args.num_filter_maps, args.lstm_dim, args.saved_dir, args.data_path,
                args.gpu, args.split_batch, args.stochastic)
    else:
        print(msg)
        sys.exit(0)

