# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import itertools
import json
import logging
import os
from argparse import Namespace
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import torch
from fairseq import metrics, utils
from fairseq.data import (AppendTokenDataset, ConcatDataset,
                          LanguagePairDataset, MultidomainCorpusSampledDataset,
                          PrependTokenDataset, StripTokenDataset,
                          TruncateDataset, data_utils, encoders,
                          indexed_dataset)
from fairseq.data.indexed_dataset import get_available_dataset_impl
from fairseq.dataclass import ChoiceEnum, FairseqDataclass
from fairseq.models import BaseActor
from fairseq.tasks import FairseqTask, register_task
from omegaconf import II
from torch.distributions import Categorical

EVAL_BLEU_ORDER = 4


logger = logging.getLogger(__name__)


def load_langpair_dataset(
    data_path,
    split,
    src,
    src_dict,
    tgt,
    tgt_dict,
    combine,
    dataset_impl,
    upsample_primary,
    left_pad_source,
    left_pad_target,
    max_source_positions,
    max_target_positions,
    prepend_bos=False,
    load_alignments=False,
    truncate_source=False,
    append_source_id=False,
    num_buckets=0,
    shuffle=True,
    pad_to_multiple=1,
):
    def split_exists(split, src, tgt, lang, data_path):
        filename = os.path.join(data_path, "{}.{}-{}.{}".format(split, src, tgt, lang))
        return indexed_dataset.dataset_exists(filename, impl=dataset_impl)

    src_datasets = []
    tgt_datasets = []

    for k in itertools.count():
        split_k = split + (str(k) if k > 0 else "")

        # infer langcode
        if split_exists(split_k, src, tgt, src, data_path):
            prefix = os.path.join(data_path, "{}.{}-{}.".format(split_k, src, tgt))
        elif split_exists(split_k, tgt, src, src, data_path):
            prefix = os.path.join(data_path, "{}.{}-{}.".format(split_k, tgt, src))
        else:
            if k > 0:
                break
            else:
                raise FileNotFoundError(
                    "Dataset not found: {} ({})".format(split, data_path)
                )

        src_dataset = data_utils.load_indexed_dataset(
            prefix + src, src_dict, dataset_impl
        )
        if truncate_source:
            src_dataset = AppendTokenDataset(
                TruncateDataset(
                    StripTokenDataset(src_dataset, src_dict.eos()),
                    max_source_positions - 1,
                ),
                src_dict.eos(),
            )
        src_datasets.append(src_dataset)

        tgt_dataset = data_utils.load_indexed_dataset(
            prefix + tgt, tgt_dict, dataset_impl
        )
        if tgt_dataset is not None:
            tgt_datasets.append(tgt_dataset)

        logger.info(
            "{} {} {}-{} {} examples".format(
                data_path, split_k, src, tgt, len(src_datasets[-1])
            )
        )

        if not combine:
            break

    assert len(src_datasets) == len(tgt_datasets) or len(tgt_datasets) == 0

    if len(src_datasets) == 1:
        src_dataset = src_datasets[0]
        tgt_dataset = tgt_datasets[0] if len(tgt_datasets) > 0 else None
    else:
        sample_ratios = [1] * len(src_datasets)
        sample_ratios[0] = upsample_primary
        src_dataset = ConcatDataset(src_datasets, sample_ratios)
        if len(tgt_datasets) > 0:
            tgt_dataset = ConcatDataset(tgt_datasets, sample_ratios)
        else:
            tgt_dataset = None

    if prepend_bos:
        assert hasattr(src_dict, "bos_index") and hasattr(tgt_dict, "bos_index")
        src_dataset = PrependTokenDataset(src_dataset, src_dict.bos())
        if tgt_dataset is not None:
            tgt_dataset = PrependTokenDataset(tgt_dataset, tgt_dict.bos())

    eos = None
    if append_source_id:
        src_dataset = AppendTokenDataset(
            src_dataset, src_dict.index("[{}]".format(src))
        )
        if tgt_dataset is not None:
            tgt_dataset = AppendTokenDataset(
                tgt_dataset, tgt_dict.index("[{}]".format(tgt))
            )
        eos = tgt_dict.index("[{}]".format(tgt))

    align_dataset = None
    if load_alignments:
        align_path = os.path.join(data_path, "{}.align.{}-{}".format(split, src, tgt))
        if indexed_dataset.dataset_exists(align_path, impl=dataset_impl):
            align_dataset = data_utils.load_indexed_dataset(
                align_path, None, dataset_impl
            )

    tgt_dataset_sizes = tgt_dataset.sizes if tgt_dataset is not None else None
    return LanguagePairDataset(
        src_dataset,
        src_dataset.sizes,
        src_dict,
        tgt_dataset,
        tgt_dataset_sizes,
        tgt_dict,
        left_pad_source=left_pad_source,
        left_pad_target=left_pad_target,
        align_dataset=align_dataset,
        eos=eos,
        num_buckets=num_buckets,
        shuffle=shuffle,
        pad_to_multiple=pad_to_multiple,
    )


@dataclass
class MultiUATMultidomainTranslationConfig(FairseqDataclass):
    data: Optional[str] = field(
        default=None,
        metadata={
            "help": "colon separated path to data directories list, will be iterated upon during epochs "
            "in round-robin manner; however, valid and test data are always in the first directory "
            "to avoid the need for repeating them in all directories",
        },
    )
    source_lang: Optional[str] = field(
        default=None,
        metadata={
            "help": "source language",
            "argparse_alias": "-s",
        },
    )
    target_lang: Optional[str] = field(
        default=None,
        metadata={
            "help": "target language",
            "argparse_alias": "-t",
        },
    )
    load_alignments: bool = field(
        default=False, metadata={"help": "load the binarized alignments"}
    )
    left_pad_source: bool = field(
        default=True, metadata={"help": "pad the source on the left"}
    )
    left_pad_target: bool = field(
        default=False, metadata={"help": "pad the target on the left"}
    )
    max_source_positions: int = field(
        default=1024, metadata={"help": "max number of tokens in the source sequence"}
    )
    max_target_positions: int = field(
        default=1024, metadata={"help": "max number of tokens in the target sequence"}
    )
    upsample_primary: int = field(
        default=-1, metadata={"help": "the amount of upsample primary dataset"}
    )
    truncate_source: bool = field(
        default=False, metadata={"help": "truncate source to max-source-positions"}
    )
    num_batch_buckets: int = field(
        default=0,
        metadata={
            "help": "if >0, then bucket source and target lengths into "
            "N buckets and pad accordingly; this is useful on TPUs to minimize the number of compilations"
        },
    )
    train_subset: str = II("dataset.train_subset")
    dataset_impl: Optional[ChoiceEnum(get_available_dataset_impl())] = II(
        "dataset.dataset_impl"
    )
    required_seq_len_multiple: int = II("dataset.required_seq_len_multiple")

    # options for reporting BLEU during validation
    eval_bleu: bool = field(
        default=False, metadata={"help": "evaluation with BLEU scores"}
    )
    eval_bleu_args: str = field(
        default="{}",
        metadata={
            "help": 'generation args for BLUE scoring, e.g., \'{"beam": 4, "lenpen": 0.6}\', as JSON string'
        },
    )
    eval_bleu_detok: str = field(
        default="space",
        metadata={
            "help": "detokenize before computing BLEU (e.g., 'moses'); required if using --eval-bleu; "
            "use 'space' to disable detokenization; see fairseq.data.encoders for other options"
        },
    )
    eval_bleu_detok_args: str = field(
        default="{}",
        metadata={"help": "args for building the tokenizer, if needed, as JSON string"},
    )
    eval_tokenized_bleu: bool = field(
        default=False, metadata={"help": "compute tokenized BLEU instead of sacrebleu"}
    )
    eval_bleu_remove_bpe: Optional[str] = field(
        default=None,
        metadata={
            "help": "remove BPE before computing BLEU",
            "argparse_const": "@@ ",
        },
    )
    eval_bleu_print_samples: bool = field(
        default=False, metadata={"help": "print sample generations during validation"}
    )

    # options for multi-domain NMT
    temperature: str = field(
        default="1",
        metadata={
            "help": "temperature for sampling probability distribution, inf for uniform, 1 for proportional"
        }
    )
    max_sample_tokens: int = field(
        default=1024, 
        metadata={
            "help": "max number of tokens in sampled batch"
        }
    )
    domain_file: str = field(
        default="",
        metadata={
            "help": "path to domain file"
        }
    )

    data_actor: str = field(
        default="base",
        metadata={
            "help": "type of data actor, base"
        }
    )

    data_actor_optim_step: int = field(
        default=200, 
        metadata={
            "help": "N steps for updating the data actor"
        }
    )

    data_actor_lr: float = field(
        default=0.0001, 
        metadata={
            "help": "learning rate for data actor"
        }
    )

    update_sampling_interval: int = field(
        default=2000, 
        metadata={
            "help": "update sample probability every N updates"
        }
    )

    sample_prob_log: str = field(
        default="",
        metadata={
            "help": "path to store the change of sampling probability"
        }
    )

    K: int = field(
        default=10, 
        metadata={
            "help": "update sample probability every N updates"
        }
    )

    reward_type: str = field(
        default="entropy",
        metadata={
            "help": "type of reward, entropy, xentropy"
        }
    )




@register_task("multiuat_multidomain_translation", dataclass=MultiUATMultidomainTranslationConfig)
class MultiUATMultidomainTranslation(FairseqTask):
    """
    Translate from one (source) language to another (target) language.

    Args:
        src_dict (~fairseq.data.Dictionary): dictionary for the source language
        tgt_dict (~fairseq.data.Dictionary): dictionary for the target language

    .. note::

        The translation task is compatible with :mod:`fairseq-train`,
        :mod:`fairseq-generate` and :mod:`fairseq-interactive`.
    """

    cfg: MultiUATMultidomainTranslationConfig

    def __init__(self, cfg: MultiUATMultidomainTranslationConfig, src_dict, tgt_dict):
        super().__init__(cfg)
        self.cfg = cfg
        self.src_dict = src_dict
        self.tgt_dict = tgt_dict
        self.domain_paths = []
        with open(self.cfg.domain_file, "r", encoding="utf-8") as f:
            for line in f.readlines():
                self.domain_paths.append(line.strip())
        self.num_domains = len(self.domain_paths)
        self.data_actor_pretrained = False
        if self.cfg.sample_prob_log is not None and os.path.exists(self.cfg.sample_prob_log):
            os.remove(self.cfg.sample_prob_log)
        self.current_sampling_update_num = 0

    @classmethod
    def setup_task(cls, cfg: MultiUATMultidomainTranslationConfig, **kwargs):
        """Setup the task (e.g., load dictionaries).

        Args:
            args (argparse.Namespace): parsed command-line arguments
        """

        paths = utils.split_paths(cfg.data)
        assert len(paths) > 0
        # find language pair automatically
        if cfg.source_lang is None or cfg.target_lang is None:
            cfg.source_lang, cfg.target_lang = data_utils.infer_language_pair(paths[0])
        if cfg.source_lang is None or cfg.target_lang is None:
            raise Exception(
                "Could not infer language pair, please provide it explicitly"
            )

        # load dictionaries
        src_dict = cls.load_dictionary(
            os.path.join(paths[0], "dict.{}.txt".format(cfg.source_lang))
        )
        tgt_dict = cls.load_dictionary(
            os.path.join(paths[0], "dict.{}.txt".format(cfg.target_lang))
        )
        assert src_dict.pad() == tgt_dict.pad()
        assert src_dict.eos() == tgt_dict.eos()
        assert src_dict.unk() == tgt_dict.unk()
        logger.info("[{}] dictionary: {} types".format(cfg.source_lang, len(src_dict)))
        logger.info("[{}] dictionary: {} types".format(cfg.target_lang, len(tgt_dict)))

        return cls(cfg, src_dict, tgt_dict)

    def load_dataset(self, split, epoch=1, combine=False, **kwargs):
        """Load a given dataset split.

        Args:
            split (str): name of the split (e.g., train, valid, test)
        """
        # paths = utils.split_paths(self.cfg.data)
        # assert len(paths) > 0
        # if split != self.cfg.train_subset:
        #     # if not training data set, use the first shard for valid and test
        #     paths = paths[:1]
        # data_path = paths[(epoch - 1) % len(paths)]

        # infer langcode
        src, tgt = self.cfg.source_lang, self.cfg.target_lang
        
        domain_splits = OrderedDict()
        self.domains = []
        for d_path in self.domain_paths:
            domain_name = os.path.basename(d_path).split("-")[0]
            self.domains.append(domain_name)
            d_dataset = load_langpair_dataset(
                d_path,
                split,
                src,
                self.src_dict,
                tgt,
                self.tgt_dict,
                combine=combine,
                dataset_impl=self.cfg.dataset_impl,
                upsample_primary=self.cfg.upsample_primary,
                left_pad_source=self.cfg.left_pad_source,
                left_pad_target=self.cfg.left_pad_target,
                max_source_positions=self.cfg.max_source_positions,
                max_target_positions=self.cfg.max_target_positions,
                load_alignments=self.cfg.load_alignments,
                truncate_source=self.cfg.truncate_source,
                num_buckets=self.cfg.num_batch_buckets,
                shuffle=(split != "test"),
                pad_to_multiple=self.cfg.required_seq_len_multiple,
            )
            domain_splits[domain_name] = d_dataset

        self.datasets[split] = MultidomainCorpusSampledDataset(self.cfg, domain_splits)
        if split == "train":
            self.datasets["train"].get_sample_prob()
            self.write_sampling_log(self.domains)
            self.write_sampling_log(self.datasets["train"].p.tolist())



    def build_dataset_for_inference(self, src_tokens, src_lengths, constraints=None):
        return LanguagePairDataset(
            src_tokens,
            src_lengths,
            self.source_dictionary,
            tgt_dict=self.target_dictionary,
            constraints=constraints,
        )

    def build_model(self, cfg):
        model = super().build_model(cfg)
        if self.cfg.eval_bleu:
            detok_args = json.loads(self.cfg.eval_bleu_detok_args)
            self.tokenizer = encoders.build_tokenizer(
                Namespace(tokenizer=self.cfg.eval_bleu_detok, **detok_args)
            )

            gen_args = json.loads(self.cfg.eval_bleu_args)
            self.sequence_generator = self.build_generator(
                [model], Namespace(**gen_args)
            )
        return model

    def build_data_actor(self, args):
        if args.data_actor == "base":
            actor = BaseActor(args, self.num_domains)
        else:
            actor = None
        return actor

    def train_step(
        self, sample, model, criterion, optimizer, update_num, 
        data_actor=None, data_optimizer=None, 
        ignore_grad=False, prepare_fn=None
    ):
        """
        Do forward and backward, and return the loss as computed by *criterion*
        for the given *model* and *sample*.

        Args:
            sample (dict): the mini-batch. The format is defined by the
                :class:`~fairseq.data.FairseqDataset`.
            model (~fairseq.models.BaseFairseqModel): the model
            criterion (~fairseq.criterions.FairseqCriterion): the criterion
            optimizer (~fairseq.optim.FairseqOptimizer): the optimizer
            update_num (int): the current update
            ignore_grad (bool): multiply loss by 0 if this is set to True

        Returns:
            tuple:
                - the loss
                - the sample size, which is used as the denominator for the
                  gradient
                - logging outputs to display while training
        """
        if not self.data_actor_pretrained and data_actor is not None:
            self.pretrain_data_actor(data_actor, data_optimizer)
            self.data_actor_pretrained = True
        
        model.train()
        model.set_num_updates(update_num)


        if update_num % self.cfg.update_sampling_interval == 0 and update_num != 0 and data_actor is not None and self.current_sampling_update_num != update_num:
            
            # train_grad_state = self.copy_grad(model)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            p = self.update_sample_probability(model, criterion, data_actor, data_optimizer, prepare_fn)
            self.datasets["train"].update_sampling_p(p)
            self.write_sampling_log(self.datasets["train"].p.tolist())
            # self.restore_grad(train_grad_state, model)
            self.current_sampling_update_num = update_num

        with torch.autograd.profiler.record_function("forward"):
            loss, sample_size, logging_output = criterion(model, sample)
        if ignore_grad:
            loss *= 0
        with torch.autograd.profiler.record_function("backward"):
            optimizer.backward(loss)

        return loss, sample_size, logging_output

    def update_sample_probability(
        self, model, criterion, data_actor, data_optimizer, prepare_fn
    ):

        logger.info("******* Update Sampling Probability *******")

        all_reward_list = []
        for i, valid_key in enumerate(self.datasets["train"].datasets.keys()):
            sample = self.datasets["valid"].get_sample_with_key(valid_key)[valid_key]
            sample, _ = prepare_fn(sample)
            if self.cfg.reward_type == "enttp":
                r = self.compute_enttp_monta_carlo(model, sample)
            elif self.cfg.reward_type == "enteos":
                r = self.compute_enteos_monta_carlo(model, sample)
            elif self.cfg.reward_type == "pretp":
                r = self.compute_pretp_monte_carlo(model, sample)
            elif self.cfg.reward_type == "exptp":
                r = self.compute_exptp_monte_carlo(model, sample)
            elif self.cfg.reward_type == "vartp":
                r = self.compute_vartp_monte_carlo(model, sample)
            elif self.cfg.reward_type == "comtp":
                r = self.compute_comtp_monte_carlo(model, sample)
            elif self.cfg.reward_type == "xentropy":
                r = self.compute_xentropy(model, criterion, sample)
                r = r / sample["ntokens"]
            else:
                raise RuntimeError("undefined reward")
            all_reward_list.append(r)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()


        # sim_list = np.mean(np.array(all_ent_list), axis=0).tolist()
        sim_list = all_reward_list
        logger.info("Rewards List: " + "\t".join([str(i) for i in sim_list]))
        feature = torch.ones(1, len(self.datasets["train"].datasets.keys()))
        grad_scale = torch.FloatTensor(sim_list).view(1, -1)
        if torch.cuda.is_available():
            feature = feature.cuda()
            grad_scale = grad_scale.cuda()
        
        for _ in range(self.cfg.data_actor_optim_step):
            data_actor.zero_grad()
            data_optimizer.zero_grad()
            a_logits = data_actor(feature)
            loss = -torch.nn.functional.log_softmax(a_logits, dim=-1)
            loss = (loss * grad_scale).sum()
            loss.backward()
            data_optimizer.step()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        with torch.no_grad():
            a_logits = data_actor(feature)
            prob = torch.nn.functional.softmax(a_logits, dim=-1)
            
        return prob.data.view(-1).cpu().numpy()
    
    def compute_xentropy(self, model, criterion, sample):
        model.eval()
        loss, sample_size, logging_output = criterion(model, sample)
        model.train()
        return loss.item()
    

    def compute_pretp_monte_carlo(self, model, sample):
        target_mask = (sample["target"] != self.tgt_dict.pad()).float()
        lst = []
        for i in range(self.cfg.K):
            net_output = model(**sample["net_input"])
            prob = model.get_normalized_probs(net_output, log_probs=True)
            prob, _ = torch.max(prob, dim=-1)
            mean_tp = torch.mean(torch.exp(torch.sum(prob * target_mask, dim=-1))).item()
            lst.append(mean_tp)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        return 1-np.mean(np.array(lst))
    
    def compute_exptp_monte_carlo(self, model, sample):
        target_mask = (sample["target"] != self.tgt_dict.pad()).float()
        lst = []
        for i in range(self.cfg.K):
            net_output = model(**sample["net_input"])
            prob = model.get_normalized_probs(net_output, log_probs=True)
            prob, _ = torch.max(prob, dim=-1)
            mean_tp = torch.sum(prob*target_mask, dim=-1) / torch.sum(target_mask, dim=-1)
            mean_tp = torch.mean(torch.exp(mean_tp)).item()
            lst.append(mean_tp)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()


        return 1-np.mean(np.array(lst))

    def compute_vartp_monte_carlo(self, model, sample):
        target_mask = (sample["target"] != self.tgt_dict.pad())
        lst = []
        for i in range(self.cfg.K):
            net_output = model(**sample["net_input"])
            prob = model.get_normalized_probs(net_output, log_probs=True)
            varlst = []
            for i in range(prob.size(0)):
                p, m = prob[i], target_mask[i]
                p, _ = torch.max(p, dim=-1)
                varlst.append(torch.var(torch.masked_select(p, m)).item())
            mean_tp = sum(varlst) / len(varlst)
            lst.append(mean_tp)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        return np.mean(np.array(lst))

    def compute_comtp_monte_carlo(self, model, sample):
        target_mask = (sample["target"] != self.tgt_dict.pad())
        lst = []
        for i in range(self.cfg.K):
            net_output = model(**sample["net_input"])
            prob = model.get_normalized_probs(net_output, log_probs=True)
            varlst = []
            for i in range(prob.size(0)):
                p, m = prob[i], target_mask[i]
                p, _ = torch.max(p, dim=-1)
                varlst.append(torch.var(torch.masked_select(p, m)).item())
            prob, _ = torch.max(prob, dim=-1)
            mean_tp = (torch.sum(prob*target_mask.float(), dim=-1) / torch.sum(target_mask.float(), dim=-1)).detach().cpu().numpy()
            mean_tp = np.mean(np.exp(np.array(varlst) / mean_tp)) 

            lst.append(mean_tp)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        return np.mean(np.array(lst))
    
    def compute_enttp_monta_carlo(self, model, sample):
        target_mask = (sample["target"] != self.tgt_dict.pad()).float()
        lst = []
        for i  in range(self.cfg.K):
            net_output = model(**sample["net_input"])
            prob = model.get_normalized_probs(net_output, log_probs=False)
            e = Categorical(probs=prob).entropy().detach()
            e = (torch.sum(e * target_mask) / torch.sum(target_mask)).cpu().numpy()
            lst.append(e)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        return np.mean(np.array(lst))

    def compute_enteos_monta_carlo(self, model, sample):
        target_mask = (sample["target"] == self.tgt_dict.eos()).float()
        lst = []
        for i  in range(self.cfg.K):
            net_output = model(**sample["net_input"])
            prob = model.get_normalized_probs(net_output, log_probs=False)
            e = Categorical(probs=prob).entropy().detach()
            e = (torch.sum(e * target_mask) / torch.sum(target_mask)).cpu().numpy()
            lst.append(e)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        return np.mean(np.array(lst))

    def pretrain_data_actor(self, data_actor, data_optimizer):
        logger.info("******* Pretrain Data Actor *******")
        feature =  torch.ones(1, len(self.datasets["train"].datasets.keys()))
        datasize_p = self.datasets["train"].p
        target = torch.FloatTensor(datasize_p).view(1, -1)
        if torch.cuda.is_available():
            feature = feature.cuda()
            target = target.cuda()
        l = 100
        count = 0
        while l > 0.00000001:
            data_actor.zero_grad()
            data_optimizer.zero_grad()
            a_logits = data_actor(feature)
            prob = torch.nn.functional.softmax(a_logits, dim=-1)
            loss = torch.nn.functional.mse_loss(prob, target)
            l = loss.item()
            if count % 1000 == 0 :
                logger.info("Pretrain Data Actor | Loss = %.7f | num_updates = %10d" % (l, count))
            loss.backward()
            data_optimizer.step()
            # grad = torch.autograd.grad(loss, filter(lambda x:x.requires_grad, data_actor.parameters()))
            # updated_weights = {n: p - self.args.data_actor_lr * 10 * g for g, (n, p) in zip(grad, data_actor.named_parameters())}
            # data_actor = self.update_params(data_actor, updated_weights)
            count += 1

        with torch.no_grad():
            a_logits = data_actor(feature)
            prob = torch.nn.functional.softmax(a_logits, dim=-1)
            sim_list = [i for i in prob.data.view(-1).cpu().numpy()]
            logger.info("******* Pretrain Complete *******")
            for x, y, z in list(zip(self.domains, self.datasets["train"].dataset_sizes, sim_list)):
                logger.info("Pretrained Data Actor | Domain: %15s | size = %8d | Sampling probability = %6.3f%% " % (x, y, z*100))

    def update_params(self, model, updated_weights):
        for n, p in model.named_parameters():
            p.data = updated_weights[n]
        return model

    def copy_grad(self, model, to_cpu=False):
        state = {}
        for n, p in model.named_parameters():
            state[n] = p.grad.data.clone()
        return state
    
    def restore_grad(self, state, model):
        for n, p in model.named_parameters():
            p.grad.data = state[n]

    def grad_cosine_sim(self, train_grad, valid_grad):
        cosine_prod, train_cosine_norm, valid_cosine_norm = 0, 0, 0
        for (nt, gt), (nv, gv) in zip(train_grad.items(), valid_grad.items()):
            assert nt == nv
            cosine_prod += (gt.data * gv.data).sum().item()
            train_cosine_norm += gt.data.norm(2) ** 2
            valid_cosine_norm += gv.data.norm(2) ** 2

        cosine_sim = cosine_prod / ((train_cosine_norm*valid_cosine_norm)**0.5 + 1e-10)
        return cosine_sim.item()

    def write_sampling_log(self, lst):
        if self.cfg.sample_prob_log is not None:
            with open(self.cfg.sample_prob_log, "a", encoding="utf-8") as f:
                f.write(",".join([str(i) for i in lst]) + "\n")



    def valid_step(self, sample, model, criterion):
        loss, sample_size, logging_output = super().valid_step(sample, model, criterion)
        if self.cfg.eval_bleu:
            bleu = self._inference_with_bleu(self.sequence_generator, sample, model)
            logging_output["_bleu_sys_len"] = bleu.sys_len
            logging_output["_bleu_ref_len"] = bleu.ref_len
            # we split counts into separate entries so that they can be
            # summed efficiently across workers using fast-stat-sync
            assert len(bleu.counts) == EVAL_BLEU_ORDER
            for i in range(EVAL_BLEU_ORDER):
                logging_output["_bleu_counts_" + str(i)] = bleu.counts[i]
                logging_output["_bleu_totals_" + str(i)] = bleu.totals[i]
        return loss, sample_size, logging_output

    def reduce_metrics(self, logging_outputs, criterion):
        super().reduce_metrics(logging_outputs, criterion)
        if self.cfg.eval_bleu:

            def sum_logs(key):
                return sum(log.get(key, 0) for log in logging_outputs)

            counts, totals = [], []
            for i in range(EVAL_BLEU_ORDER):
                counts.append(sum_logs("_bleu_counts_" + str(i)))
                totals.append(sum_logs("_bleu_totals_" + str(i)))

            if max(totals) > 0:
                # log counts as numpy arrays -- log_scalar will sum them correctly
                metrics.log_scalar("_bleu_counts", np.array(counts))
                metrics.log_scalar("_bleu_totals", np.array(totals))
                metrics.log_scalar("_bleu_sys_len", sum_logs("_bleu_sys_len"))
                metrics.log_scalar("_bleu_ref_len", sum_logs("_bleu_ref_len"))

                def compute_bleu(meters):
                    import inspect

                    import sacrebleu

                    fn_sig = inspect.getfullargspec(sacrebleu.compute_bleu)[0]
                    if "smooth_method" in fn_sig:
                        smooth = {"smooth_method": "exp"}
                    else:
                        smooth = {"smooth": "exp"}
                    bleu = sacrebleu.compute_bleu(
                        correct=meters["_bleu_counts"].sum,
                        total=meters["_bleu_totals"].sum,
                        sys_len=meters["_bleu_sys_len"].sum,
                        ref_len=meters["_bleu_ref_len"].sum,
                        **smooth
                    )
                    return round(bleu.score, 2)

                metrics.log_derived("bleu", compute_bleu)

    def max_positions(self):
        """Return the max sentence length allowed by the task."""
        return (self.cfg.max_source_positions, self.cfg.max_target_positions)

    @property
    def source_dictionary(self):
        """Return the source :class:`~fairseq.data.Dictionary`."""
        return self.src_dict

    @property
    def target_dictionary(self):
        """Return the target :class:`~fairseq.data.Dictionary`."""
        return self.tgt_dict

    def _inference_with_bleu(self, generator, sample, model):
        import sacrebleu

        def decode(toks, escape_unk=False):
            s = self.tgt_dict.string(
                toks.int().cpu(),
                self.cfg.eval_bleu_remove_bpe,
                # The default unknown string in fairseq is `<unk>`, but
                # this is tokenized by sacrebleu as `< unk >`, inflating
                # BLEU scores. Instead, we use a somewhat more verbose
                # alternative that is unlikely to appear in the real
                # reference, but doesn't get split into multiple tokens.
                unk_string=("UNKNOWNTOKENINREF" if escape_unk else "UNKNOWNTOKENINHYP"),
            )
            if self.tokenizer:
                s = self.tokenizer.decode(s)
            return s

        gen_out = self.inference_step(generator, [model], sample, prefix_tokens=None)
        hyps, refs = [], []
        for i in range(len(gen_out)):
            hyps.append(decode(gen_out[i][0]["tokens"]))
            refs.append(
                decode(
                    utils.strip_pad(sample["target"][i], self.tgt_dict.pad()),
                    escape_unk=True,  # don't count <unk> as matches to the hypo
                )
            )
        if self.cfg.eval_bleu_print_samples:
            logger.info("example hypothesis: " + hyps[0])
            logger.info("example reference : " + refs[0])
        if self.cfg.eval_tokenized_bleu:
            return sacrebleu.corpus_bleu(hyps, [refs], tokenize="none")
        else:
            return sacrebleu.corpus_bleu(hyps, [refs])
