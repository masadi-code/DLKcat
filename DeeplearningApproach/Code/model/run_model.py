#!/usr/bin/python
# coding: utf-8

# Author: LE YUAN
# Date: 2020-10-23

import argparse
import pickle
import sys
import timeit
import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from sklearn.metrics import mean_squared_error,r2_score


class KcatPrediction(nn.Module):
    def __init__(self, n_fingerprint, n_word, dim, layer_gnn, window, layer_cnn, layer_output):
        super(KcatPrediction, self).__init__()
        self.embed_fingerprint = nn.Embedding(n_fingerprint, dim)
        self.embed_word = nn.Embedding(n_word, dim)
        self.W_gnn = nn.ModuleList([nn.Linear(dim, dim)
                                    for _ in range(layer_gnn)])
        self.W_cnn = nn.ModuleList([nn.Conv2d(
                     in_channels=1, out_channels=1, kernel_size=2*window+1,
                     stride=1, padding=window) for _ in range(layer_cnn)])
        self.W_attention = nn.Linear(dim, dim)
        self.W_out = nn.ModuleList([nn.Linear(2*dim, 2*dim)
                                    for _ in range(layer_output)])
        # self.W_interaction = nn.Linear(2*dim, 2)
        self.W_interaction = nn.Linear(2*dim, 1)

    def gnn(self, xs, A):
        for G in self.W_gnn:
            hs = torch.relu(G(xs))
            xs = xs + torch.matmul(A, hs)
        # return torch.unsqueeze(torch.sum(xs, 0), 0)
        return torch.unsqueeze(torch.mean(xs, 0), 0)

    def attention_cnn(self, x, xs):
        """The attention mechanism is applied to the last layer of CNN."""

        xs = torch.unsqueeze(torch.unsqueeze(xs, 0), 0)
        for C in self.W_cnn:
            xs = F.leaky_relu(C(xs))
        xs = torch.squeeze(torch.squeeze(xs, 0), 0)

        h = torch.relu(self.W_attention(x))
        hs = torch.relu(self.W_attention(xs))
        weights = torch.tanh(F.linear(h, hs))
        ys = torch.t(weights) * hs

        # return torch.unsqueeze(torch.sum(ys, 0), 0)
        return torch.unsqueeze(torch.mean(ys, 0), 0)

    def forward(self, inputs):

        fingerprints, adjacency, words = inputs

        """Compound vector with GNN."""
        fingerprint_vectors = self.embed_fingerprint(fingerprints)
        compound_vector = self.gnn(fingerprint_vectors, adjacency)

        """Protein vector with attention-CNN."""
        word_vectors = self.embed_word(words)
        protein_vector = self.attention_cnn(compound_vector, word_vectors)

        """Concatenate the above two vectors and output the interaction."""
        cat_vector = torch.cat((compound_vector, protein_vector), 1)
        for L in self.W_out:
            cat_vector = torch.relu(L(cat_vector))
        interaction = self.W_interaction(cat_vector)
        # print(interaction)

        return interaction

    def __call__(self, data, train=True):

        inputs, correct_interaction = data[:-1], data[-1]
        predicted_interaction = self.forward(inputs)
        # print(predicted_interaction)

        if train:
            loss = F.mse_loss(predicted_interaction, correct_interaction)
            correct_values = correct_interaction.to('cpu').data.numpy()
            predicted_values = predicted_interaction.to('cpu').data.numpy()[0]
            return loss, correct_values, predicted_values
        else:
            correct_values = correct_interaction.to('cpu').data.numpy()
            predicted_values = predicted_interaction.to('cpu').data.numpy()[0]
            # correct_values = np.concatenate(correct_values)
            # predicted_values = np.concatenate(predicted_values)
            # ys = F.softmax(predicted_interaction, 1).to('cpu').data.numpy()
            # predicted_values = list(map(lambda x: np.argmax(x), ys))
            # print(correct_values)
            # print(predicted_values)
            # predicted_scores = list(map(lambda x: x[1], ys))
            return correct_values, predicted_values


class Trainer(object):
    def __init__(self, model, lr, weight_decay, batch_size=1):
        self.model = model
        self.batch_size = batch_size
        self.optimizer = optim.Adam(self.model.parameters(),
                                    lr=lr, weight_decay=weight_decay)

    def train(self, dataset):
        np.random.shuffle(dataset)
        N = len(dataset)
        loss_total = 0
        trainCorrect, trainPredict = [], []
        self.optimizer.zero_grad()
        for i, data in enumerate(dataset):
            loss, correct_values, predicted_values = self.model(data)
            loss.backward()
            if self.batch_size == 1 or ((i+1)%self.batch_size == 0) or i+1 == N:
                self.optimizer.step()
                self.optimizer.zero_grad()
            loss_total += loss.to('cpu').data.numpy()

            correct_values = math.log10(math.pow(2,correct_values))
            predicted_values = math.log10(math.pow(2,predicted_values))
            trainCorrect.append(correct_values)
            trainPredict.append(predicted_values)
        rmse_train = np.sqrt(mean_squared_error(trainCorrect,trainPredict))
        r2_train = r2_score(trainCorrect,trainPredict)
        return loss_total, rmse_train, r2_train


class Tester(object):
    def __init__(self, model):
        self.model = model

    def test(self, dataset):
        N = len(dataset)
        SAE = 0  # sum absolute error.
        testY, testPredict = [], []
        for data in dataset :
            (correct_values, predicted_values) = self.model(data, train=False)
            correct_values = math.log10(math.pow(2,correct_values))
            predicted_values = math.log10(math.pow(2,predicted_values))
            SAE += np.abs(predicted_values-correct_values)
            # SAE += sum(np.abs(predicted_values-correct_values))
            testY.append(correct_values)
            testPredict.append(predicted_values)
        MAE = SAE / N  # mean absolute error.
        rmse = np.sqrt(mean_squared_error(testY,testPredict))
        r2 = r2_score(testY,testPredict)
        return MAE, rmse, r2

    def save_MAEs(self, MAEs, filename):
        with open(filename, 'a') as f:
            f.write('\t'.join(map(str, MAEs)) + '\n')

    def save_model(self, model, filename):
        torch.save(model.state_dict(), filename)

def load_tensor(file_name, dtype, device):
    return [dtype(d).to(device) for d in np.load(file_name + '.npy', allow_pickle=True)]


def load_pickle(file_name):
    with open(file_name, 'rb') as f:
        return pickle.load(f)

def shuffle_dataset(dataset, seed):
    np.random.seed(seed)
    np.random.shuffle(dataset)
    return dataset

def split_dataset(dataset, ratio):
    n = int(ratio * len(dataset))
    dataset_1, dataset_2 = dataset[:n], dataset[n:]
    return dataset_1, dataset_2


def train_model(args):
    """Hyperparameters."""
    dim = args.dim
    layer_gnn = args.layer_gnn
    window = args.window
    layer_cnn = args.layer_cnn
    layer_output = args.layer_output
    lr = args.lr
    lr_decay = args.lr_decay
    decay_interval = args.decay_interval
    weight_decay = args.weight_decay
    iteration = args.iterations

    # print(type(radius))

    """CPU or GPU."""
    if torch.cuda.is_available():
        device = torch.device('cuda')
        print('The code uses GPU...')
    else:
        device = torch.device('cpu')
        print('The code uses CPU!!!')

    """Load preprocessed data."""
    dir_input = ('../../Data/input/')
    compounds = load_tensor(dir_input + 'compounds', torch.LongTensor, device)
    adjacencies = load_tensor(dir_input + 'adjacencies', torch.FloatTensor, device)
    proteins = load_tensor(dir_input + 'proteins', torch.LongTensor, device)
    interactions = load_tensor(dir_input + 'regression', torch.FloatTensor, device)
    fingerprint_dict = load_pickle(dir_input + 'fingerprint_dict.pickle')
    word_dict = load_pickle(dir_input + 'sequence_dict.pickle')
    n_fingerprint = len(fingerprint_dict)
    n_word = len(word_dict)
    # print(n_fingerprint)  # 3958
    # print(n_word)  # 8542
    # 394 and 474 when radius=1 and ngram=2

    """Create a dataset and split it into train/dev/test."""
    dataset = list(zip(compounds, adjacencies, proteins, interactions))
    dataset = shuffle_dataset(dataset, 1234)
    dataset_train, dataset_ = split_dataset(dataset, 0.8)
    dataset_dev, dataset_test = split_dataset(dataset_, 0.5)

    """Set a model."""
    torch.manual_seed(1234)
    model = KcatPrediction(n_fingerprint, n_word, dim, layer_gnn, window, layer_cnn, layer_output).to(device)
    trainer = Trainer(model, lr, weight_decay, batch_size=args.batch_size)
    tester = Tester(model)

    args_vars = vars(args)
    args_strs = [str(k)+str(v) for (k, v) in args_vars.items() if k != "run_name"]
    setting = args.run_name + '-' + str.join('_', args_strs)
    """Output files."""
    file_MAEs = '../../Results/output/MAEs--' + setting + '.txt'
    file_model = '../../Results/output/' + setting
    MAEs = ('Epoch\tTime(sec)\tRMSE_train\tR2_train\tMAE_dev\tMAE_test\tRMSE_dev\tRMSE_test\tR2_dev\tR2_test')
    with open(file_MAEs, 'w') as f:
        f.write(MAEs + '\n')

    """Start training."""
    print('Training...')
    print(MAEs)
    start = timeit.default_timer()

    for epoch in range(1, iteration+1):

        if epoch % decay_interval == 0:
            trainer.optimizer.param_groups[0]['lr'] *= lr_decay

        loss_train, rmse_train, r2_train = trainer.train(dataset_train)
        MAE_dev, RMSE_dev, R2_dev = tester.test(dataset_dev)
        MAE_test, RMSE_test, R2_test = tester.test(dataset_test)

        end = timeit.default_timer()
        time = end - start

        MAEs = [epoch, time, rmse_train, r2_train, MAE_dev,
                MAE_test, RMSE_dev, RMSE_test, R2_dev, R2_test]
        tester.save_MAEs(MAEs, file_MAEs)
        tester.save_model(model, file_model)

        print('\t'.join(map(str, MAEs)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # (DATASET, radius, ngram, dim, layer_gnn, window, layer_cnn, layer_output,
    # lr, lr_decay, decay_interval, weight_decay, iteration,
    # setting)
    parser.add_argument("run_name")
    parser.add_argument("--dim", type=int, default=20)
    parser.add_argument("--layer-gnn", type=int, default=3)
    parser.add_argument("--window", type=int, default=11)
    parser.add_argument("--layer-cnn", type=int, default=3)
    parser.add_argument("--layer-output", type=int, default=3)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--lr-decay", type=float, default=0.5)
    parser.add_argument("--decay-interval", type=int, default=10)
    parser.add_argument("--weight-decay", type=float, default=1e-6)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=1)
    args = parser.parse_args()

    train_model(args)
