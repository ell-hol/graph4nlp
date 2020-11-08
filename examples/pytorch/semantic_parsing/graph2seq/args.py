import argparse, yaml

from graph4nlp.pytorch.modules.config import get_basic_args

def update_values(dict_from, dict_to):
    for key, value in dict_from.items():
        if isinstance(value, dict) and key in dict_to.keys():
            update_values(dict_from[key], dict_to[key])
        elif value is not None:
            dict_to[key] = dict_from[key]


def get_yaml_config(config_path="config.yml"):
    with open(config_path, "r") as setting:
        config = yaml.load(setting)
    return config


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_yaml", type=str,
                        default="examples/pytorch/semantic_parsing/graph2seq/config/new_dependency.yaml", help="")
    parser.add_argument('--word-emb-size', type=int, default=300, help='')
    parser.add_argument("--log-file", type=str, default="examples/pytorch/semantic_parsing/graph2seq/log/ggnn.txt")
    parser.add_argument("--checkpoint-save-path", type=str, default="examples/pytorch/semantic_parsing/graph2seq/save")

    parser.add_argument('--learning-rate', type=float, default=1e-3, help='')
    parser.add_argument("--loss-display-step", type=int, default=3, help=' ')
    parser.add_argument("--eval-display-number", type=int, default=3, help="")
    parser.add_argument("--lr-start-decay-epoch", type=int, default=20, help="")
    parser.add_argument("--lr-decay-rate", type=float, default=0.9)
    parser.add_argument("--lr-decay-per-epoch", type=int, default=5, help="")
    parser.add_argument("--min-lr", type=float, default=1e-3, help="")
    parser.add_argument("--use-gpu", type=float, default=1, help="0 for don't use cuda, 1 for using cuda")
    parser.add_argument("--gpu", type=int, default=0, help="gpu id")
    parser.add_argument("--seed", type=int, default=1236, help="")

    # dataset config
    parser.add_argument("--batch_size", type=int, default=24, help="the size of one mini-batch")
    parser.add_argument("--share-vocab", type=bool, default=True, help="whether to share vocab")
    parser.add_argument("--val_split_ratio", type=float, default=0, help="")
    parser.add_argument("--pretrained_word_emb_file", type=str,
                        default='/home/shiina/shiina/lib/graph4nlp/.vector_cache/glove.6B.300d.txt', help="")

    parser.add_argument("--beam-size", type=int, default=4, help="the beam size of beam search")

    cfg = parser.parse_args()

    our_args = get_yaml_config(cfg.dataset_yaml)
    template = get_basic_args(graph_construction_name=our_args["graph_construction_name"],
                              graph_embedding_name=our_args["graph_embedding_name"],
                              decoder_name=our_args["decoder_name"])
    update_values(our_args, template)
    update_values(template, vars(cfg))

    # dataset_args = get_yaml_config(cfg.dataset_yaml)
    # update_values(dataset_args, vars(cfg))
    return cfg
