import os
import random
import time
import pickle
import argparse
import copy

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import torch.optim as optim
from torch.utils.data import DataLoader

from graph4nlp.pytorch.data.data import to_batch
from graph4nlp.pytorch.datasets.jobs import JobsDatasetForTree
from graph4nlp.pytorch.modules.graph_construction import *
from graph4nlp.pytorch.modules.graph_embedding import *
from graph4nlp.pytorch.models.graph2tree import Graph2Tree
from graph4nlp.pytorch.modules.utils.tree_utils import Tree

import warnings
warnings.filterwarnings('ignore')


class Jobs:
    def __init__(self, opt=None):
        super(Jobs, self).__init__()
        self.opt = opt

        seed = opt.seed
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)

        if opt.gpuid == -1:
            self.device = torch.device("cpu")
        else:
            self.device = torch.device("cuda:{}".format(opt.gpuid))

        self.use_copy = True if opt.use_copy == 1 else False
        self.use_share_vocab = opt.use_share_vocab
        self.data_dir = opt.data_dir

        self._build_dataloader()
        self._build_model()
        self._build_optimizer()

    def _build_dataloader(self):
        if self.opt.graph_construction_type == "DependencyGraph":
            dataset = JobsDatasetForTree(root_dir=self.data_dir,
                                         topology_builder=DependencyBasedGraphConstruction,
                                         topology_subdir='DependencyGraph', 
                                         edge_strategy='as_node',
                                         share_vocab=self.use_share_vocab, 
                                         enc_emb_size=self.opt.enc_emb_size,
                                         dec_emb_size=self.opt.tgt_emb_size,
                                         min_word_vocab_freq=self.opt.min_freq)

        elif self.opt.graph_construction_type == "ConstituencyGraph":
            dataset = JobsDatasetForTree(root_dir=self.data_dir,
                                         topology_builder=ConstituencyBasedGraphConstruction,
                                         topology_subdir='ConstituencyGraph', 
                                         share_vocab=self.use_share_vocab,
                                         enc_emb_size=self.opt.enc_emb_size, 
                                         dec_emb_size=self.opt.tgt_emb_size,
                                         min_word_vocab_freq=self.opt.min_freq)

        elif self.opt.graph_construction_type == "DynamicGraph_node_emb":
            dataset = JobsDatasetForTree(root_dir=self.data_dir, 
                                         word_emb_size=self.opt.enc_emb_size,
                                         topology_builder=NodeEmbeddingBasedGraphConstruction,
                                         topology_subdir='DynamicGraph_node_emb', 
                                         graph_type='dynamic',
                                         dynamic_graph_type='node_emb', 
                                         share_vocab=self.use_share_vocab,
                                         enc_emb_size=self.opt.enc_emb_size, 
                                         dec_emb_size=self.opt.tgt_emb_size,
                                         min_word_vocab_freq=self.opt.min_freq)

        elif self.opt.graph_construction_type == "DynamicGraph_node_emb_refined":
            if self.opt.dynamic_init_graph_type is None or self.opt.dynamic_init_graph_type == 'line':
                dynamic_init_topology_builder = None
            elif self.opt.dynamic_init_graph_type == 'dependency':
                dynamic_init_topology_builder = DependencyBasedGraphConstruction
            elif self.opt.dynamic_init_graph_type == 'constituency':
                dynamic_init_topology_builder = ConstituencyBasedGraphConstruction
            else:
                raise RuntimeError(
                    'Define your own dynamic_init_topology_builder')
            dataset = JobsDatasetForTree(root_dir=self.data_dir,
                                         word_emb_size=self.opt.enc_emb_size,
                                         topology_builder=NodeEmbeddingBasedRefinedGraphConstruction,
                                         topology_subdir='DynamicGraph_node_emb_refined', 
                                         graph_type='dynamic',
                                         dynamic_graph_type='node_emb_refined',
                                         share_vocab=self.use_share_vocab,
                                         enc_emb_size=self.opt.enc_emb_size, 
                                         dec_emb_size=self.opt.tgt_emb_size,
                                         dynamic_init_topology_builder=dynamic_init_topology_builder,
                                         min_word_vocab_freq=self.opt.min_freq)
        else:
            raise NotImplementedError

        self.train_data_loader = DataLoader(dataset.train, batch_size=self.opt.batch_size, shuffle=True, num_workers=1,
                                           collate_fn=dataset.collate_fn)
        self.test_data_loader = DataLoader(dataset.test, batch_size=1, shuffle=False, num_workers=1,
                                          collate_fn=dataset.collate_fn)
        self.src_vocab = dataset.src_vocab_model
        self.tgt_vocab = dataset.tgt_vocab_model
        if self.use_share_vocab:
            self.share_vocab = dataset.share_vocab_model

    def _build_model(self):
        '''For encoder-decoder'''
        self.embedding_style = {'single_token_item': True,
                           'emb_strategy': "w2v_bilstm",
                           'num_rnn_layers': 1
                           }
        self.criterion = nn.NLLLoss(size_average=False)
        self.model = Graph2Tree.from_args(self.opt, 
                                          self.src_vocab, 
                                          self.tgt_vocab, 
                                          self.device, 
                                          self.embedding_style,
                                          self.criterion)
        self.model.init(self.opt.init_weight)
        self.model.to(self.device)

    def _build_optimizer(self):
        optim_state = {"learningRate": self.opt.learning_rate, "weight_decay": self.opt.weight_decay}
        parameters = [p for p in self.model.parameters() if p.requires_grad]
        self.optimizer = optim.Adam(parameters, lr=optim_state['learningRate'], weight_decay=optim_state['weight_decay'])

    def prepare_ext_vocab(self, batch_graph, src_vocab):
        oov_dict = copy.deepcopy(src_vocab)
        token_matrix = []
        for n in batch_graph.node_attributes:
            node_token = n['token']
            if (n.get('type') == None or n.get('type') == 0) and oov_dict.get_symbol_idx(node_token) == oov_dict.get_symbol_idx(oov_dict.unk_token):
                oov_dict.add_symbol(node_token)
            token_matrix.append(oov_dict.get_symbol_idx(node_token))
        batch_graph.node_features['token_id_oov'] = torch.tensor(token_matrix, dtype=torch.long).to(self.device)
        return oov_dict

    def train_epoch(self, epoch):
        from graph4nlp.pytorch.modules.utils.copy_utils import prepare_ext_vocab
        loss_to_print = 0
        num_batch = len(self.train_data_loader)
        for step, data in enumerate(self.train_data_loader):
            batch_graph, batch_tree_list, batch_original_tree_list = data['graph_data'], data['dec_tree_batch'], data['original_dec_tree_batch']
            batch_graph = batch_graph.to(self.device)
            self.optimizer.zero_grad()
            oov_dict = self.prepare_ext_vocab(
                batch_graph, self.src_vocab) if self.use_copy else None

            if self.use_copy:
                batch_tree_list_refined = []
                for item in batch_original_tree_list:
                    tgt_list = oov_dict.get_symbol_idx_for_list(item.strip().split())
                    tgt_tree = Tree.convert_to_tree(tgt_list, 0, len(tgt_list), oov_dict)
                    batch_tree_list_refined.append(tgt_tree)
            loss = self.model(batch_graph, batch_tree_list_refined if self.use_copy else batch_tree_list, oov_dict=oov_dict)
            loss.backward()
            torch.nn.utils.clip_grad_value_(
                self.model.parameters(), self.opt.grad_clip)
            self.optimizer.step()
            loss_to_print += loss
        return loss_to_print/num_batch

    def train(self):
        best_acc = -1

        print("-------------\nStarting training.")
        for epoch in range(1, self.opt.max_epochs+1):
            self.model.train()
            loss_to_print = self.train_epoch(epoch)
            print("epochs = {}, train_loss = {:.3f}".format(epoch, loss_to_print))
            if epoch > 2 and epoch % 5 == 0:
                test_acc = self.eval((self.model))
                if test_acc > best_acc:
                    best_acc = test_acc
        print("Best Acc: {:.3f}\n".format(best_acc))
        return best_acc

    def eval(self, model):
        from .evaluation import convert_to_string, compute_tree_accuracy
        model.eval()
        reference_list = []
        candidate_list = []
        for data in self.test_data_loader:
            eval_input_graph, batch_tree_list, batch_original_tree_list = data['graph_data'], data['dec_tree_batch'], data['original_dec_tree_batch']
            eval_input_graph = eval_input_graph.to(model.device)
            oov_dict = self.prepare_ext_vocab(eval_input_graph, self.src_vocab)

            if self.use_copy:
                assert len(batch_original_tree_list) == 1
                reference = oov_dict.get_symbol_idx_for_list(batch_original_tree_list[0].split())
                eval_vocab = oov_dict
            else:
                assert len(batch_original_tree_list) == 1
                reference = model.tgt_vocab.get_symbol_idx_for_list(batch_original_tree_list[0].split())
                eval_vocab = self.tgt_vocab

            candidate = model.decoder.translate(model.use_copy,
                                                model.decoder.enc_hidden_size,
                                                model.decoder.hidden_size,
                                                model,
                                                eval_input_graph,
                                                self.src_vocab,
                                                self.tgt_vocab,
                                                model.device,
                                                self.opt.max_dec_seq_length,
                                                self.opt.max_dec_tree_depth,
                                                oov_dict=oov_dict,
                                                use_beam_search=True,
                                                beam_size=self.opt.beam_size)
            
            candidate = [int(c) for c in candidate]
            num_left_paren = sum(
                1 for c in candidate if eval_vocab.idx2symbol[int(c)] == "(")
            num_right_paren = sum(
                1 for c in candidate if eval_vocab.idx2symbol[int(c)] == ")")
            diff = num_left_paren - num_right_paren
            if diff > 0:
                for i in range(diff):
                    candidate.append(
                        self.test_data_loader.tgt_vocab.symbol2idx[")"])
            elif diff < 0:
                candidate = candidate[:diff]
            ref_str = convert_to_string(
                reference, eval_vocab)
            cand_str = convert_to_string(
                candidate, eval_vocab)

            reference_list.append(reference)
            candidate_list.append(candidate)
        test_acc = compute_tree_accuracy(
            candidate_list, reference_list, eval_vocab)
        print("TEST ACCURACY = {:.3f}\n".format(test_acc))
        return test_acc

if __name__ == "__main__":
    start = time.time()
    main_arg_parser = argparse.ArgumentParser(description="parser")

    main_arg_parser.add_argument('-gpuid', type=int, default=1, help='which gpu to use. -1 = use CPU')
    main_arg_parser.add_argument('-seed', type=int, default=1234, help='torch manual random number generator seed')
    main_arg_parser.add_argument('-use_copy', type=int, default=1, help='whether use copy mechanism')
    main_arg_parser.add_argument('-data_dir', type=str, default='/home/lishucheng/Graph4AI/graph4nlp/examples/pytorch/semantic_parsing/graph2tree/data/jobs', help='data path')

    main_arg_parser.add_argument('-gnn_type', type=str, default="SAGE")
    main_arg_parser.add_argument('-gat_head', type=str, default="1")
    main_arg_parser.add_argument('-sage_aggr', type=str, default="lstm")
    main_arg_parser.add_argument('-attn_type', type=str, default="uniform")
    main_arg_parser.add_argument('-use_sibling', type=int, default=0)
    main_arg_parser.add_argument('-use_share_vocab', type=int, default=1)
    main_arg_parser.add_argument('-K', type=int, default=1)

    main_arg_parser.add_argument('-enc_emb_size', type=int, default=300)
    main_arg_parser.add_argument('-tgt_emb_size', type=int, default=300)

    main_arg_parser.add_argument('-enc_hidden_size', type=int, default=300)
    main_arg_parser.add_argument('-dec_hidden_size', type=int, default=300)

    # DynamicGraph_node_emb_refined, DynamicGraph_node_emb, ConstituencyGraph
    main_arg_parser.add_argument('-graph_construction_type', type=str, default="DynamicGraph_node_emb")

    # "None, line, dependency, constituency"
    main_arg_parser.add_argument('-dynamic_init_graph_type', type=str, default="constituency")
    main_arg_parser.add_argument('-batch_size', type=int, default=20)
    main_arg_parser.add_argument('-dropout_for_word_embedding', type=float, default=0.1)
    main_arg_parser.add_argument('-dropout_for_encoder', type=float, default=0)
    main_arg_parser.add_argument('-dropout_for_decoder', type=float, default=0.1)

    main_arg_parser.add_argument('-direction_option', type=str, default="undirected")
    main_arg_parser.add_argument('-beam_size', type=int, default=2)

    main_arg_parser.add_argument('-max_dec_seq_length', type=int, default=50)
    main_arg_parser.add_argument('-max_dec_tree_depth', type=int, default=50)

    main_arg_parser.add_argument('-teacher_force_ratio', type=float, default=1.0)
    main_arg_parser.add_argument('-init_weight', type=float, default=0.08, help='initailization weight')
    main_arg_parser.add_argument('-learning_rate', type=float, default=1e-3, help='learning rate')
    main_arg_parser.add_argument('-weight_decay', type=float, default=0)
    main_arg_parser.add_argument('-max_epochs', type=int, default=200,help='number of full passes through the training data')
    main_arg_parser.add_argument('-min_freq', type=int, default=1,help='minimum frequency for vocabulary')
    main_arg_parser.add_argument('-grad_clip', type=int, default=5, help='clip gradients at this value')

    args = main_arg_parser.parse_args()
    import json
    # with open("./jobs_config", "w") as f:
    #     config_jobs = json.loads(,indent=4)
    with open("./jobs_config", "w") as f:
        jsObj = json.dumps(vars(args), indent=4)
        f.write(jsObj)

    runner = Jobs(opt=args)
    best_acc = runner.train()

    end = time.time()
    print("total time: {} minutes\n".format((end - start)/60))