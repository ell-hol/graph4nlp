from dgl.nn import GraphConv

import torch
import torch.nn as nn
import dgl.function as fn
from torch.nn import init
from .base import GNNLayerBase, GNNBase
from ...data.data import GraphData
from dgl.utils import expand_as_pair

class GCN(GNNBase):
    r"""Multi-layer GCN.

    Parameters
    ----------
    """
    def __init__(self,
                 num_layers,
                 in_feats,
                 hidden_size,
                 out_feats,
                 direction_option='bi_sep',
                 norm='both',
                 weight=True,
                 bias=True,
                 activation=None,
                 allow_zero_in_degree=False,
                 use_edge_weight=False):
        super(GCN, self).__init__()
        self.num_layers = num_layers
        self.direction_option = direction_option
        self.gcn_layers = nn.ModuleList()
        assert self.num_layers > 0
        self.use_edge_weight = use_edge_weight

        if isinstance(hidden_size, int):
            hidden_size = [hidden_size] * (self.num_layers - 1)

        if self.num_layers > 1:
            # input projection
            self.gcn_layers.append(GCNLayer(in_feats,
                                            hidden_size[0],
                                            direction_option=self.direction_option,
                                            norm=norm,
                                            weight=weight,
                                            bias=bias,
                                            activation=activation,
                                            allow_zero_in_degree=allow_zero_in_degree))

        # hidden layers
        for l in range(1, self.num_layers - 1):
            # due to multi-head, the input_size = hidden_size * num_heads
            self.gcn_layers.append(GCNLayer(hidden_size[l - 1],
                                            hidden_size[l],
                                            direction_option=self.direction_option,
                                            norm=norm,
                                            weight=weight,
                                            bias=bias,
                                            activation=activation,
                                            allow_zero_in_degree=allow_zero_in_degree))
        # output projection
        self.gcn_layers.append(GCNLayer(hidden_size[-1] if self.num_layers > 1 else in_feats,
                                        out_feats,
                                        direction_option=self.direction_option,
                                        norm=norm,
                                        weight=weight,
                                        bias=bias,
                                        activation=activation,
                                        allow_zero_in_degree=allow_zero_in_degree))

    def forward(self, graph):
        r"""Compute multi-layer graph attention network.

        Parameters
        ----------
        graph : GraphData
            The graph data containing topology and features.

        Returns
        -------
        GraphData
            The output graph data containing updated embeddings.
        """
        feat = graph.node_features['node_feat']
        dgl_graph = graph.to_dgl()

        if self.direction_option == 'bi_sep':
            h = [feat, feat]
        else:
            h = feat

        if self.use_edge_weight:
            edge_weight = graph.edge_features['edge_weight']
            if self.direction_option != 'undirected':
                reverse_edge_weight = graph.edge_features['reverse_edge_weight']
            else:
                reverse_edge_weight = None
        else:
            edge_weight = None
            reverse_edge_weight = None

        for l in range(self.num_layers - 1):
            h = self.gcn_layers[l](dgl_graph, h, edge_weight=edge_weight, reverse_edge_weight=reverse_edge_weight)
            if self.direction_option == 'bi_sep':
                h = [each.flatten(1) for each in h]
            else:
                h = h.flatten(1)

        # output projection
        logits = self.gcn_layers[-1](dgl_graph, h)

        if self.direction_option == 'bi_sep':
            # logits = [each.mean(1) for each in logits]
            logits = torch.cat(logits, -1)
        else:
            pass

        graph.node_features['node_emb'] = logits

        return graph

class GCNLayer(GNNLayerBase):
    r"""Single-layer GCN.

    Parameters
    ----------
    input_size : int, or pair of ints
        Input feature size.
        If the layer is to be applied to a unidirectional bipartite graph, ``input_size``
        specifies the input feature size on both the source and destination nodes.  If
        a scalar is given, the source and destination node feature size would take the
        same value.
    output_size : int
        Output feature size.
    num_heads : int
        Number of heads in Multi-Head Attention.
    direction_option: str
        Whether use unidirectional (i.e., regular) or bidirectional (i.e., `bi_sep` and `bi_fuse`) versions.
        Default: ``'bi_sep'``.
    feat_drop : float, optional
        Dropout rate on feature, default: ``0``.
    attn_drop : float, optional
        Dropout rate on attention weight, default: ``0``.
    negative_slope : float, optional
        LeakyReLU angle of negative slope, default: ``0.2``.
    residual : bool, optional
        If True, use residual connection.
        Default: ``False``.
    activation : callable activation function/layer or None, optional.
        If not None, applies an activation function to the updated node features.
        Default: ``None``.
    """
    def __init__(self,
                 in_feats,
                 out_feats,
                 direction_option='bi_sep',
                 norm='both',
                 weight=True,
                 bias=True,
                 activation=None,
                 allow_zero_in_degree=False):
        super(GCNLayer, self).__init__()
        if direction_option == 'undirected':
            self.model = UndirectedGCNLayerConv( in_feats,
                                                 out_feats,
                                                 norm=norm,
                                                 weight=weight,
                                                 bias=bias,
                                                 activation=activation,
                                                 allow_zero_in_degree=allow_zero_in_degree)
        elif direction_option == 'bi_sep':
            self.model = BiSepGCNLayerConv(  in_feats,
                                             out_feats,
                                             norm=norm,
                                             weight=weight,
                                             bias=bias,
                                             activation=activation,
                                             allow_zero_in_degree=allow_zero_in_degree)
        elif direction_option == 'bi_fuse':
            self.model = BiFuseGCNLayerConv( in_feats,
                                             out_feats,
                                             norm=norm,
                                             weight=weight,
                                             bias=bias,
                                             activation=activation,
                                             allow_zero_in_degree=allow_zero_in_degree)
        else:
            raise RuntimeError('Unknown `direction_option` value: {}'.format(direction_option))

    def forward(self, graph, feat, weight=None, edge_weight=None, reverse_edge_weight=None):
        r"""Compute graph attention network layer.

        Parameters
        ----------
        graph : DGLGraph
            The graph.
        feat : torch.Tensor or pair of torch.Tensor
            If a torch.Tensor is given, the input feature of shape :math:`(N, D_{in})` where
            :math:`D_{in}` is size of input feature, :math:`N` is the number of nodes.
            If a pair of torch.Tensor is given, the pair must contain two tensors of shape
            :math:`(N_{in}, D_{in_{src}})` and :math:`(N_{out}, D_{in_{dst}})`.

        Returns
        -------
        torch.Tensor
            The output feature of shape :math:`(N, H, D_{out})` where :math:`H`
            is the number of heads, and :math:`D_{out}` is size of output feature.
        """
        return self.model(graph, feat, weight, edge_weight, reverse_edge_weight)


class UndirectedGCNLayerConv(GNNLayerBase):
    r"""

    Description
    -----------
    Graph convolution was introduced in `GCN <https://arxiv.org/abs/1609.02907>`__
    and mathematically is defined as follows:

    .. math::
      h_i^{(l+1)} = \sigma(b^{(l)} + \sum_{j\in\mathcal{N}(i)}\frac{1}{c_{ij}}h_j^{(l)}W^{(l)})

    where :math:`\mathcal{N}(i)` is the set of neighbors of node :math:`i`,
    :math:`c_{ij}` is the product of the square root of node degrees
    (i.e.,  :math:`c_{ij} = \sqrt{|\mathcal{N}(i)|}\sqrt{|\mathcal{N}(j)|}`),
    and :math:`\sigma` is an activation function.

    .. math::
       h_{i}^{0} & = [ x_i \| \mathbf{0} ]

       a_{i}^{t} & = \sum_{j\in\mathcal{N}(i)} W_{e_{ij}} h_{j}^{t}

       h_{i}^{t+1} & = \mathrm{GRU}(a_{i}^{t}, h_{i}^{t})

    Parameters
    ----------
    in_feats : int
        Input feature size; i.e, the number of dimensions of :math:`h_j^{(l)}`.
    out_feats : int
        Output feature size; i.e., the number of dimensions of :math:`h_i^{(l+1)}`.
    norm : str, optional
        How to apply the normalizer. If is `'right'`, divide the aggregated messages
        by each node's in-degrees, which is equivalent to averaging the received messages.
        If is `'none'`, no normalization is applied. Default is `'both'`,
        where the :math:`c_{ij}` in the paper is applied.
    weight : bool, optional
        If True, apply a linear layer. Otherwise, aggregating the messages
        without a weight matrix.
    bias : bool, optional
        If True, adds a learnable bias to the output. Default: ``True``.
    activation : callable activation function/layer or None, optional
        If not None, applies an activation function to the updated node features.
        Default: ``None``.
    allow_zero_in_degree : bool, optional
        If there are 0-in-degree nodes in the graph, output for those nodes will be invalid
        since no message will be passed to those nodes. This is harmful for some applications
        causing silent performance regression. This module will raise a DGLError if it detects
        0-in-degree nodes in input graph. By setting ``True``, it will suppress the check
        and let the users handle it by themselves. Default: ``False``.

    Attributes
    ----------
    weight : torch.Tensor
        The learnable weight tensor.
    bias : torch.Tensor
        The learnable bias tensor.
    """

    def __init__(self,
                 in_feats,
                 out_feats,
                 norm='both',
                 weight=True,
                 bias=True,
                 activation=None,
                 allow_zero_in_degree=False):
        super(UndirectedGCNLayerConv, self).__init__()
        if norm not in ('none', 'both', 'right'):
            raise RuntimeError('Invalid norm value. Must be either "none", "both" or "right".'
                               ' But got "{}".'.format(norm))
        self._in_feats = in_feats
        self._out_feats = out_feats
        self._norm = norm
        self._allow_zero_in_degree = allow_zero_in_degree

        if weight:
            self.weight = nn.Parameter(torch.Tensor(in_feats, out_feats))
        else:
            self.register_parameter('weight', None)

        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_feats))
        else:
            self.register_parameter('bias', None)

        self.reset_parameters()

        self._activation = activation

    def reset_parameters(self):
        r"""
    
        Description
        -----------
        Reinitialize learnable parameters.
    
        Note
        ----
        The model parameters are initialized as in the
        `original implementation <https://github.com/tkipf/gcn/blob/master/gcn/layers.py>`__
        where the weight :math:`W^{(l)}` is initialized using Glorot uniform initialization
        and the bias is initialized to be zero.
    
        """
        if self.weight is not None:
            init.xavier_uniform_(self.weight)
        if self.bias is not None:
            init.zeros_(self.bias)
    
    def set_allow_zero_in_degree(self, set_value):
        r"""
    
        Description
        -----------
        Set allow_zero_in_degree flag.
    
        Parameters
        ----------
        set_value : bool
            The value to be set to the flag.
        """
        self._allow_zero_in_degree = set_value

    def forward(self, graph, feat, weight=None, edge_weight=None, reverse_edge_weight=None):
        r"""Compute graph convolution.

        Notes
        -----
        * Input shape: :math:`(N, *, \text{in_feats})` where * means any number of additional
          dimensions, :math:`N` is the number of nodes.
        * Output shape: :math:`(N, *, \text{out_feats})` where all but the last dimension are
          the same shape as the input.
        * Weight shape: "math:`(\text{in_feats}, \text{out_feats})`.

        Parameters
        ----------
        graph : DGLGraph
            The graph.
        feat : torch.Tensor
            The input feature
        weight : torch.Tensor, optional
            Optional external weight tensor.

        Returns
        -------
        torch.Tensor
            The output feature
        """
        assert reverse_edge_weight is None
        graph = graph.local_var()

        if self._norm == 'both':
            degs = graph.out_degrees().to(feat.device).float().clamp(min=1)
            norm = torch.pow(degs, -0.5)
            shp = norm.shape + (1,) * (feat.dim() - 1)
            norm = torch.reshape(norm, shp)
            feat = feat * norm

        if weight is not None:
            if self.weight is not None:
                raise RuntimeError('External weight is provided while at the same time the'
                                   ' module has defined its own weight parameter. Please'
                                   ' create the module with flag weight=False.')
        else:
            weight = self.weight

        if self._in_feats > self._out_feats:
            # mult W first to reduce the feature size for aggregation.
            if weight is not None:
                feat = torch.matmul(feat, weight)
            graph.srcdata['h'] = feat
            if edge_weight is None:
                graph.update_all(fn.copy_src(src='h', out='m'),
                                 fn.sum(msg='m', out='h'))
            else:
                graph.edata['edge_weight'] = edge_weight
                graph.update_all(fn.u_mul_e('h', 'edge_weight', 'm'),
                                 fn.sum('m', 'h'))
            rst = graph.dstdata['h']
        else:
            # aggregate first then mult W
            graph.srcdata['h'] = feat
            if edge_weight is None:
                graph.update_all(fn.copy_src(src='h', out='m'),
                                 fn.sum(msg='m', out='h'))
            else:
                graph.edata['edge_weight'] = edge_weight
                graph.update_all(fn.u_mul_e('h', 'edge_weight', 'm'),
                                 fn.sum('m', 'h'))
            rst = graph.dstdata['h']
            if weight is not None:
                rst = torch.matmul(rst, weight)

        if self._norm != 'none':
            degs = graph.in_degrees().to(feat.device).float().clamp(min=1)
            if self._norm == 'both':
                norm = torch.pow(degs, -0.5)
            else:
                norm = 1.0 / degs
            shp = norm.shape + (1,) * (feat.dim() - 1)
            norm = torch.reshape(norm, shp)
            rst = rst * norm

        if self.bias is not None:
            rst = rst + self.bias

        if self._activation is not None:
            rst = self._activation(rst)

        return rst


    def extra_repr(self):
        """Set the extra representation of the module,
        which will come into effect when printing the model.
        """
        summary = 'in={_in_feats}, out={_out_feats}'
        summary += ', normalization={_norm}'
        if '_activation' in self.__dict__:
            summary += ', activation={_activation}'
        return summary.format(**self.__dict__)


class BiFuseGCNLayerConv(GNNLayerBase):
    r"""Bidirection version GCN layer from paper `GCN <https://arxiv.org/abs/1609.02907>`__.

    .. math::
        h_{\mathcal{N}(i)}^{(l+1)} & = \mathrm{aggregate}
        \left(\{h_{j}^{l}, \forall j \in \mathcal{N}(i) \}\right)
        h_{i}^{(l+1)} & = \sigma \left(W \cdot \mathrm{concat}
        (h_{i}^{l}, h_{\mathcal{N}(i)}^{l+1} + b) \right)
        h_{i}^{(l+1)} & = \mathrm{norm}(h_{i}^{l})
    Parameters
    ----------
    input_size : int, or pair of ints
        Input feature size.
        If the layer is to be applied on a unidirectional bipartite graph, ``in_feats``
        specifies the input feature size on both the source and destination nodes.  If
        a scalar is given, the source and destination node feature size would take the
        same value.
        If aggregator type is ``gcn``, the feature size of source and destination nodes
        are required to be the same.
    output_size : int
        Output feature size.
    feat_drop : float
        Dropout rate on features, default: ``0``.
    aggregator_type : str
        Aggregator type to use (``mean``, ``gcn``, ``pool``, ``lstm``).
    bias : bool
        If True, adds a learnable bias to the output. Default: ``True``.
    norm : callable activation function/layer or None, optional
        If not None, applies normalization to the updated node features.
    activation : callable activation function/layer or None, optional
        If not None, applies an activation function to the updated node features.
        Default: ``None``.
    """

    def __init__(self,
                 in_feats,
                 out_feats,
                 norm='both',
                 weight=True,
                 bias=True,
                 activation=None,
                 allow_zero_in_degree=False):
        super(BiFuseGCNLayerConv, self).__init__()
        if norm not in ('none', 'both', 'right'):
            raise RuntimeError('Invalid norm value. Must be either "none", "both" or "right".'
                               ' But got "{}".'.format(norm))
        self._in_feats = in_feats
        self._out_feats = out_feats
        self._norm = norm
        self._allow_zero_in_degree = allow_zero_in_degree

        if weight:
            self.weight_fw = nn.Parameter(torch.Tensor(in_feats, out_feats))
            self.weight_bw = nn.Parameter(torch.Tensor(in_feats, out_feats))
        else:
            self.register_parameter('weight_fw', None)
            self.register_parameter('weight_bw', None)

        if bias:
            self.bias_fw = nn.Parameter(torch.Tensor(out_feats))
            self.bias_bw = nn.Parameter(torch.Tensor(out_feats))
        else:
            self.register_parameter('bias_fw', None)
            self.register_parameter('bias_bw', None)

        self.reset_parameters()

        self._activation = activation

        self.fuse_linear = nn.Linear(4 * out_feats, out_feats, bias=True)

    def reset_parameters(self):
        r"""
        Reinitialize learnable parameters.
        """
        if self.weight_fw is not None:
            init.xavier_uniform_(self.weight_fw)
            init.xavier_uniform_(self.weight_bw)
        if self.bias_fw is not None:
            init.zeros_(self.bias_fw)
            init.zeros_(self.bias_bw)

    def set_allow_zero_in_degree(self, set_value):
        r"""

        Description
        -----------
        Set allow_zero_in_degree flag.

        Parameters
        ----------
        set_value : bool
            The value to be set to the flag.
        """
        self._allow_zero_in_degree = set_value

    def forward(self, graph, feat, weight=None, edge_weight=None, reverse_edge_weight=None):
        r"""

        Description
        -----------
        Compute graph convolution.

        Parameters
        ----------
        graph : DGLGraph
            The graph.
        feat : torch.Tensor or pair of torch.Tensor
            If a torch.Tensor is given, it represents the input feature of shape
            :math:`(N, D_{in})`
            where :math:`D_{in}` is size of input feature, :math:`N` is the number of nodes.
            If a pair of torch.Tensor is given, which is the case for bipartite graph, the pair
            must contain two tensors of shape :math:`(N_{in}, D_{in_{src}})` and
            :math:`(N_{out}, D_{in_{dst}})`.
        weight_fw, weight_bw : torch.Tensor, optional
            Optional external weight tensor.

        Returns
        -------
        torch.Tensor
            The output feature
        """
        feat_fw = feat_bw = feat
        if isinstance(weight, tuple):
            weight_fw, weight_bw = weight
        else:
            weight_fw = weight_bw = weight

        # forward direction
        with graph.local_scope():
            graph = graph.local_var()

            if self._norm == 'both':
                degs = graph.out_degrees().to(feat_fw.device).float().clamp(min=1)
                norm = torch.pow(degs, -0.5)
                shp = norm.shape + (1,) * (feat_fw.dim() - 1)
                norm = torch.reshape(norm, shp)
                feat_fw = feat_fw * norm

            if weight_fw is not None:
                if self.weight_fw is not None:
                    raise RuntimeError('External weight is provided while at the same time the'
                                       ' module has defined its own weight parameter. Please'
                                       ' create the module with flag weight=False.')
            else:
                weight_fw = self.weight_fw

            if self._in_feats > self._out_feats:
                # mult W first to reduce the feature size for aggregation.
                if weight_fw is not None:
                    feat_fw = torch.matmul(feat_fw, weight_fw)
                graph.srcdata['h'] = feat_fw
                if edge_weight is None:
                    graph.update_all(fn.copy_src(src='h', out='m'),
                                     fn.sum(msg='m', out='h'))
                else:
                    graph.edata['edge_weight'] = edge_weight
                    graph.update_all(fn.u_mul_e('h', 'edge_weight', 'm'),
                                     fn.sum('m', 'h'))
                rst_fw = graph.dstdata['h']
            else:
                # aggregate first then mult W
                graph.srcdata['h'] = feat_fw
                if edge_weight is None:
                    graph.update_all(fn.copy_src(src='h', out='m'),
                                     fn.sum(msg='m', out='h'))
                else:
                    graph.edata['edge_weight'] = edge_weight
                    graph.update_all(fn.u_mul_e('h', 'edge_weight', 'm'),
                                     fn.sum('m', 'h'))
                rst_fw = graph.dstdata['h']
                if weight_fw is not None:
                    rst_fw = torch.matmul(rst_fw, weight_fw)

            if self._norm != 'none':
                degs = graph.in_degrees().to(feat_fw.device).float().clamp(min=1)
                if self._norm == 'both':
                    norm = torch.pow(degs, -0.5)
                else:
                    norm = 1.0 / degs
                shp = norm.shape + (1,) * (feat_fw.dim() - 1)
                norm = torch.reshape(norm, shp)
                rst_fw = rst_fw * norm

            if self.bias_fw is not None:
                rst_fw = rst_fw + self.bias_fw

            if self._activation is not None:
                rst_fw = self._activation(rst_fw)

        # backward direction
        graph = graph.reverse()
        with graph.local_scope():
            graph = graph.local_var()

            if self._norm == 'both':
                degs = graph.out_degrees().to(feat_bw.device).float().clamp(min=1)
                norm = torch.pow(degs, -0.5)
                shp = norm.shape + (1,) * (feat_bw.dim() - 1)
                norm = torch.reshape(norm, shp)
                feat_bw = feat_bw * norm

            if weight_bw is not None:
                if self.weight_bw is not None:
                    raise RuntimeError('External weight is provided while at the same time the'
                                       ' module has defined its own weight parameter. Please'
                                       ' create the module with flag weight=False.')
            else:
                weight_bw = self.weight_bw

            if self._in_feats > self._out_feats:
                # mult W first to reduce the feature size for aggregation.
                if weight_bw is not None:
                    feat_bw = torch.matmul(feat_bw, weight_bw)
                graph.srcdata['h'] = feat_bw
                if reverse_edge_weight is None:
                    graph.update_all(fn.copy_src(src='h', out='m'),
                                     fn.sum(msg='m', out='h'))
                else:
                    graph.edata['reverse_edge_weight'] = reverse_edge_weight
                    graph.update_all(fn.u_mul_e('h', 'reverse_edge_weight', 'm'),
                                     fn.sum('m', 'h'))
                rst_bw = graph.dstdata['h']
            else:
                # aggregate first then mult W
                graph.srcdata['h'] = feat_bw
                if edge_weight is None:
                    graph.update_all(fn.copy_src(src='h', out='m'),
                                     fn.sum(msg='m', out='h'))
                else:
                    graph.edata['reverse_edge_weight'] = reverse_edge_weight
                    graph.update_all(fn.u_mul_e('h', 'reverse_edge_weight', 'm'),
                                     fn.sum('m', 'h'))
                rst_bw = graph.dstdata['h']
                if weight_bw is not None:
                    rst_bw = torch.matmul(rst_bw, weight_bw)

            if self._norm != 'none':
                degs = graph.in_degrees().to(feat_bw.device).float().clamp(min=1)
                if self._norm == 'both':
                    norm = torch.pow(degs, -0.5)
                else:
                    norm = 1.0 / degs
                shp = norm.shape + (1,) * (feat_bw.dim() - 1)
                norm = torch.reshape(norm, shp)
                rst_bw = rst_bw * norm

            if self.bias_bw is not None:
                rst_bw = rst_bw + self.bias_bw

            if self._activation is not None:
                rst_bw = self._activation(rst_bw)

        fuse_vector = torch.cat(
            [rst_fw, rst_bw, rst_fw * rst_bw, rst_fw - rst_bw], dim=-1)
        fuse_gate_vector = torch.sigmoid(self.fuse_linear(fuse_vector))
        rst = fuse_gate_vector * rst_fw + (1 - fuse_gate_vector) * rst_bw

        # if self._activation is not None:
        #     rst = self._activation(rst)

        return rst


class BiSepGCNLayerConv(GNNLayerBase):
    r"""Bidirection version GCN layer from paper `GCN <https://arxiv.org/abs/1609.02907>`__.

    .. math::
        h_{\mathcal{N}(i)}^{(l+1)} & = \mathrm{aggregate}
        \left(\{h_{j}^{l}, \forall j \in \mathcal{N}(i) \}\right)
        h_{i}^{(l+1)} & = \sigma \left(W \cdot \mathrm{concat}
        (h_{i}^{l}, h_{\mathcal{N}(i)}^{l+1} + b) \right)
        h_{i}^{(l+1)} & = \mathrm{norm}(h_{i}^{l})
    """
    def __init__(self,
                 in_feats,
                 out_feats,
                 norm='both',
                 weight=True,
                 bias=True,
                 activation=None,
                 allow_zero_in_degree=False):
        super(BiSepGCNLayerConv, self).__init__()
        if norm not in ('none', 'both', 'right'):
            raise RuntimeError('Invalid norm value. Must be either "none", "both" or "right".'
                               ' But got "{}".'.format(norm))
        self._in_feats = in_feats
        self._out_feats = out_feats
        self._norm = norm
        self._allow_zero_in_degree = allow_zero_in_degree

        if weight:
            self.weight_fw = nn.Parameter(torch.Tensor(in_feats, out_feats))
            self.weight_bw = nn.Parameter(torch.Tensor(in_feats, out_feats))
        else:
            self.register_parameter('weight_fw', None)
            self.register_parameter('weight_bw', None)

        if bias:
            self.bias_fw = nn.Parameter(torch.Tensor(out_feats))
            self.bias_bw = nn.Parameter(torch.Tensor(out_feats))
        else:
            self.register_parameter('bias_fw', None)
            self.register_parameter('bias_bw', None)

        self.reset_parameters()

        self._activation = activation

    def reset_parameters(self):
        r"""
        Reinitialize learnable parameters.
        """
        if self.weight_fw is not None:
            init.xavier_uniform_(self.weight_fw)
            init.xavier_uniform_(self.weight_bw)
        if self.bias_fw is not None:
            init.zeros_(self.bias_fw)
            init.zeros_(self.bias_bw)

    def set_allow_zero_in_degree(self, set_value):
        r"""

        Description
        -----------
        Set allow_zero_in_degree flag.

        Parameters
        ----------
        set_value : bool
            The value to be set to the flag.
        """
        self._allow_zero_in_degree = set_value

    def forward(self, graph, feat, weight=None, edge_weight=None, reverse_edge_weight=None):
        r"""

        Description
        -----------
        Compute graph convolution.

        Parameters
        ----------
        graph : DGLGraph
            The graph.
        feat : torch.Tensor or pair of torch.Tensor
            If a torch.Tensor is given, it represents the input feature of shape
            :math:`(N, D_{in})`
            where :math:`D_{in}` is size of input feature, :math:`N` is the number of nodes.
            If a pair of torch.Tensor is given, which is the case for bipartite graph, the pair
            must contain two tensors of shape :math:`(N_{in}, D_{in_{src}})` and
            :math:`(N_{out}, D_{in_{dst}})`.
        weight_fw, weight_bw : torch.Tensor, optional
            Optional external weight tensor.

        Returns
        -------
        torch.Tensor
            The output feature
        """
        feat_fw, feat_bw = feat
        if isinstance(weight, tuple):
            weight_fw, weight_bw = weight
        else:
            weight_fw = weight_bw = weight

        # forward direction
        with graph.local_scope():
            graph = graph.local_var()

            if self._norm == 'both':
                degs = graph.out_degrees().to(feat_fw.device).float().clamp(min=1)
                norm = torch.pow(degs, -0.5)
                shp = norm.shape + (1,) * (feat_fw.dim() - 1)
                norm = torch.reshape(norm, shp)
                feat_fw = feat_fw * norm

            if weight_fw is not None:
                if self.weight_fw is not None:
                    raise RuntimeError('External weight is provided while at the same time the'
                                       ' module has defined its own weight parameter. Please'
                                       ' create the module with flag weight=False.')
            else:
                weight_fw = self.weight_fw

            if self._in_feats > self._out_feats:
                # mult W first to reduce the feature size for aggregation.
                if weight_fw is not None:
                    feat_fw = torch.matmul(feat_fw, weight_fw)
                graph.srcdata['h'] = feat_fw
                if edge_weight is None:
                    graph.update_all(fn.copy_src(src='h', out='m'),
                                     fn.sum(msg='m', out='h'))
                else:
                    graph.edata['edge_weight'] = edge_weight
                    graph.update_all(fn.u_mul_e('h', 'edge_weight', 'm'),
                                     fn.sum('m', 'h'))
                rst_fw = graph.dstdata['h']
            else:
                # aggregate first then mult W
                graph.srcdata['h'] = feat_fw
                if edge_weight is None:
                    graph.update_all(fn.copy_src(src='h', out='m'),
                                     fn.sum(msg='m', out='h'))
                else:
                    graph.edata['edge_weight'] = edge_weight
                    graph.update_all(fn.u_mul_e('h', 'edge_weight', 'm'),
                                     fn.sum('m', 'h'))
                rst_fw = graph.dstdata['h']
                if weight_fw is not None:
                    rst_fw = torch.matmul(rst_fw, weight_fw)

            if self._norm != 'none':
                degs = graph.in_degrees().to(feat_fw.device).float().clamp(min=1)
                if self._norm == 'both':
                    norm = torch.pow(degs, -0.5)
                else:
                    norm = 1.0 / degs
                shp = norm.shape + (1,) * (feat_fw.dim() - 1)
                norm = torch.reshape(norm, shp)
                rst_fw = rst_fw * norm

            if self.bias_fw is not None:
                rst_fw = rst_fw + self.bias_fw

            if self._activation is not None:
                rst_fw = self._activation(rst_fw)

        # backward direction
        graph = graph.reverse()
        with graph.local_scope():
            graph = graph.local_var()

            if self._norm == 'both':
                degs = graph.out_degrees().to(feat_bw.device).float().clamp(min=1)
                norm = torch.pow(degs, -0.5)
                shp = norm.shape + (1,) * (feat_bw.dim() - 1)
                norm = torch.reshape(norm, shp)
                feat_bw = feat_bw * norm

            if weight_bw is not None:
                if self.weight_bw is not None:
                    raise RuntimeError('External weight is provided while at the same time the'
                                       ' module has defined its own weight parameter. Please'
                                       ' create the module with flag weight=False.')
            else:
                weight_bw = self.weight_bw

            if self._in_feats > self._out_feats:
                # mult W first to reduce the feature size for aggregation.
                if weight_bw is not None:
                    feat_bw = torch.matmul(feat_bw, weight_bw)
                graph.srcdata['h'] = feat_bw
                if reverse_edge_weight is None:
                    graph.update_all(fn.copy_src(src='h', out='m'),
                                     fn.sum(msg='m', out='h'))
                else:
                    graph.edata['reverse_edge_weight'] = reverse_edge_weight
                    graph.update_all(fn.u_mul_e('h', 'reverse_edge_weight', 'm'),
                                     fn.sum('m', 'h'))
                rst_bw = graph.dstdata['h']
            else:
                # aggregate first then mult W
                graph.srcdata['h'] = feat_bw
                if reverse_edge_weight is None:
                    graph.update_all(fn.copy_src(src='h', out='m'),
                                     fn.sum(msg='m', out='h'))
                else:
                    graph.edata['reverse_edge_weight'] = edge_weight
                    graph.update_all(fn.u_mul_e('h', 'reverse_edge_weight', 'm'),
                                     fn.sum('m', 'h'))
                rst_bw = graph.dstdata['h']
                if weight_bw is not None:
                    rst_bw = torch.matmul(rst_bw, weight_bw)

            if self._norm != 'none':
                degs = graph.in_degrees().to(feat_bw.device).float().clamp(min=1)
                if self._norm == 'both':
                    norm = torch.pow(degs, -0.5)
                else:
                    norm = 1.0 / degs
                shp = norm.shape + (1,) * (feat_bw.dim() - 1)
                norm = torch.reshape(norm, shp)
                rst_bw = rst_bw * norm

            if self.bias_bw is not None:
                rst_bw = rst_bw + self.bias_bw

            if self._activation is not None:
                rst_bw = self._activation(rst_bw)

        return [rst_fw, rst_bw]