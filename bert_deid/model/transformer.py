"""Class for applying BERT-deid on text."""
import os
import re
import logging
from hashlib import sha256
from dataclasses import astuple, dataclass, fields
from typing import List, Optional, Union, TextIO

import numpy as np
import pandas as pd
from tqdm import tqdm

import torch
from torch.nn import CrossEntropyLoss
from torch.utils.data import (
    DataLoader, RandomSampler, SequentialSampler, TensorDataset
)

from transformers import (
    AutoConfig,
    AutoModelForTokenClassification,
    AutoTokenizer,
    set_seed,
)

from bert_deid.processors import InputFeatures, TokenClassificationTask, Split

# custom class written for albert token classification
from bert_deid import tokenization, processors
from bert_deid.label import LabelCollection, LABEL_SETS, LABEL_MEMBERSHIP
from bert_deid.processors import InputExample

logger = logging.getLogger(__name__)


@dataclass
class InputFeatures:
    """
    A single set of features of data.
    Property names are the same names as the corresponding inputs to a model.
    """

    input_ids: List[int]
    attention_mask: List[int]
    token_type_ids: Optional[List[int]] = None
    label_ids: Optional[List[int]] = None
    input_subwords: Optional[List[int]] = None
    offsets: Optional[List[int]] = None
    lengths: Optional[List[int]] = None


class Transformer(object):
    """Wrapper for a Transformer model to be applied for NER."""
    def __init__(
        self,
        model_path,
        # token_step_size=100,
        # sequence_length=100,
        max_seq_length=128,
        device='cpu',
    ):
        # by default, we do non-overlapping segments of text
        # self.token_step_size = token_step_size
        # sequence_length is how long each example for the model is
        # self.sequence_length = sequence_length

        # get the definition classes for the model
        training_args = torch.load(
            os.path.join(model_path, 'training_args.bin')
        )

        # task applied
        # TODO: figure out how to load this from saved model
        label_set = LabelCollection('i2b2_2014', transform='simple')
        self.token_classification_task = processors.DeidProcessor(
            data_dir='', label_set=label_set
        )

        # Load pretrained model and tokenizer
        self.config = AutoConfig.from_pretrained(model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            # we always use fast tokenizers as we need offsets from tokenization
            use_fast=True,
        )
        self.model = AutoModelForTokenClassification.from_pretrained(
            model_path,
            from_tf=False,
            config=self.config,
        )

        # max seq length is what we pad the model to
        # max seq length should always be >= sequence_length + 2
        self.max_seq_length = self.config.max_position_embeddings

        label_map = self.config.id2label
        self.labels = [label_map[i] for i in range(len(label_map))]
        self.num_labels = len(self.labels)

        # Use cross entropy ignore index as padding label id so
        # that only real label ids contribute to the loss later
        # TODO: get this from the model
        self.pad_token_label_id = CrossEntropyLoss().ignore_index

        # prepare the model for evaluation
        # CPU probably faster, avoids overhead
        self.device = torch.device(device)
        self.model.to(self.device)

    def split_by_overlap(self, text, token_step_size=20, sequence_length=100):
        # track offsets in tokenization
        tokens, tokens_sw, tokens_idx = self.tokenizer.tokenize_with_index(text)

        if len(tokens_idx) == 0:
            # no tokens found, return empty list
            return []
        # get start index of each token
        tokens_start = [x[0] for x in tokens_idx]
        tokens_start = np.array(tokens_start)

        # forward fill index for first token over its subsequent subword tokens
        # this means that if we try to split on a subword token, we will actually
        # split on the starting word
        mask = np.array(tokens_sw) == 1
        idx = np.where(~mask, np.arange(mask.shape[0]), 0)
        np.maximum.accumulate(idx, axis=0, out=idx)
        tokens_start[mask] = tokens_start[idx[mask]]

        if len(tokens) <= sequence_length:
            # very short text - only create one example
            seq_offsets = [[tokens_start[0], len(text)]]
        else:
            seq_offsets = range(
                0,
                len(tokens) - sequence_length, token_step_size
            )
            last_offset = seq_offsets[-1] + token_step_size
            seq_offsets = [
                [tokens_start[x], tokens_start[x + sequence_length]]
                for x in seq_offsets
            ]

            # last example always goes to the end of the text
            seq_offsets.append([tokens_start[last_offset], len(text)])

        # turn our offsets into examples
        # create a list of lists, each sub-list has 4 elements:
        #   sentence number, start index, end index, text of the sentence
        examples = list()

        for i, (start, stop) in enumerate(seq_offsets):
            examples.append([i, start, stop, text[start:stop]])

        return examples

    def _split_text_into_segments(self, text, feature_overlap=None):
        """Splits text into overlapping segments based on the model sequence length."""
        # tokenize the example text
        encoded = self.tokenizer._tokenizer.encode(
            text, add_special_tokens=False
        )
        tokens = encoded.tokens
        ids = encoded.ids
        token_sw = [False] + [
            encoded.words[i + 1] == encoded.words[i]
            for i in range(len(encoded.words) - 1)
        ]

        # prepare to split long segments into multiple sequences
        start_idx = np.array(encoded.offsets)

        # remove subwords as we do not want to split on them
        start_idx = start_idx[~np.array(token_sw), :]

        # if overlapping in sequences, get length of each subseq
        if feature_overlap is None:
            seq_len = self.tokenizer.max_len_single_sentence
        else:
            seq_len = int(
                (1 - feature_overlap) *
                (self.tokenizer.max_len_single_sentence)
            )

        # identify the starting offsets for each sub-sequence
        new_seq_idx = np.floor(
            start_idx[:, 0] / self.tokenizer.max_len_single_sentence
        ).astype(int)
        _, new_seq_idx = np.unique(new_seq_idx, return_index=True)
        new_seq_idx = start_idx[new_seq_idx, :]
        n_subseq = new_seq_idx.shape[0]

        # add the length of the text as the end of the final subsequence
        new_seq_idx = np.row_stack([new_seq_idx, [len(text), 0]])

        # iterate through subsequences and add to examples
        inputs = []

        seq_start_offset = 0
        for i in range(n_subseq):
            seq_start_offset = new_seq_idx[i, 0]
            text_subseq = text[seq_start_offset:new_seq_idx[i + 1, 0]]
            encoded = self.tokenizer._tokenizer.encode(text_subseq)
            encoded.pad(self.tokenizer.model_max_length)
            token_sw = [False] + [
                encoded.words[i + 1] == encoded.words[i]
                for i in range(len(encoded.words) - 1)
            ]
            inputs.append(
                InputFeatures(
                    input_ids=encoded.ids,
                    attention_mask=encoded.attention_mask,
                    token_type_ids=encoded.type_ids,
                    input_subwords=token_sw,
                    # note the offsets are based off the original text, not the subseq
                    offsets=[o[0] + seq_start_offset for o in encoded.offsets],
                    lengths=[o[1] - o[0] for o in encoded.offsets]
                )
            )
        return inputs

    def _features_to_tensor(self, inputs):
        """Extracts tensor datasets from a list of InputFeatures"""

        # create tensor datasets for model input
        input_ids = torch.tensor(
            [x.input_ids for x in inputs], dtype=torch.long
        )
        attention_mask = torch.tensor(
            [x.attention_mask for x in inputs], dtype=torch.long
        )
        token_type_ids = torch.tensor(
            [x.token_type_ids for x in inputs], dtype=torch.long
        )
        return input_ids, attention_mask, token_type_ids

    def _logits_to_standoff(self, logits, inputs, ignore_label='O'):
        """Converts prediction logits to stand-off prediction labels."""
        # mask, offsets, lengths
        # convert logits to probabilities

        # extract most likely label for each token
        # TODO: convert logit to prob with softmax
        # prob is used to decide between overlapping labels later
        pred_id = np.argmax(logits, axis=2)
        pred_prob = np.max(logits, axis=2)

        # re-align the predictions with the original text
        # across each sub sequence..
        labels = []
        for i in range(logits.shape[0]):
            # extract mask for valid tokens, offsets, and lengths
            mask, offsets, lengths = inputs[i].attention_mask, inputs[
                i].offsets, inputs[i].lengths
            subwords = inputs[i].input_subwords

            # increment the lengths for the first token in words tokenized into subwords
            # this ensures a prediction covers the subsequent subwords
            # it assumes that predictions are made for the first token in sub-word tokens
            # TODO: check the model only predicts a label for first sub-word token
            lengths = inputs[i].lengths
            for j in reversed(range(len(subwords))):
                if subwords[j]:
                    # cumulatively sums lengths for subwords until the first subword token
                    lengths[j - 1] += lengths[j]

            pred_label = [self.config.id2label[p] for p in pred_id[i, :]]
            # ignore object labels
            idxObject = np.asarray([p == ignore_label
                                    for p in pred_label]).astype(bool)

            # keep a subset of the token labels
            idxKeep = np.where((mask == 1) & idxObject)[0]

            labels.extend(
                [
                    [
                        pred_prob[i, p], pred_label[p], offsets[i, p],
                        lengths[i, p]
                    ] for p in idxKeep
                ]
            )

        # now, we may have multiple predictions for the same offset token
        # this can happen as we are overlapping observations to maximize
        # context for tokens near the window edges
        # so we take the *last* prediction, because that prediction will have
        # the most context

        # np.unique returns index of first unique value, so reverse the list
        offsets = [l[2] for l in labels]
        offsets.reverse()
        _, unique_idx = np.unique(offsets, return_index=True)
        unique_idx = len(offsets) - unique_idx - 1
        labels = [labels[i] for i in unique_idx]

        return labels

    def predict(self, text, batch_size=8, num_workers=0, feature_overlap=None):
        # sets the model to evaluation mode to fix parameters
        self.model.eval()

        # create a dictionary with inputs to the model
        # each element is a list of the sequence data
        inputs = self._split_text_into_segments(text, feature_overlap)
        input_ids, attention_mask, token_type_ids = self._features_to_tensor(
            inputs
        )

        with torch.no_grad():
            logits = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids
            )[0]

        logits = logits.detach().cpu().numpy()
        preds = self._logits_to_standoff(logits, inputs, ignore_label='O')

        # returns a list of the predictions with the token span
        return preds

    """
    def apply(self, text, repl='___'):
        preds, lengths, offsets = self.predict(text)

        # get the free-text label
        labels = [
            self.label_set.id_to_label[idxMax]
            for idxMax in preds.argmax(axis=1)
        ]

        # merge entities which are adjacent
        #removed_entities = np.zeros(len(labels), dtype=bool)
        for i in reversed(range(len(labels))):
            if i == 0 or labels[i] == 'O':
                continue

            if labels[i] == labels[i - 1]:
                offset, length = offsets.pop(i), lengths.pop(i)
                lengths[i - 1] = offset + length - offsets[i - 1]
                labels.pop(i)

        #keep_entities = ~removed_entities
        #labels = [l for i, l in enumerate(labels) if keep_entities[i]]
        #lengths = lengths[keep_entities]
        #offsets = offsets[keep_entities]
        for i in reversed(range(len(labels))):
            if labels[i] != 'O':
                # replace this instance of text with three underscores
                text = text[:offsets[i]] + repl + text[offsets[i] + lengths[i]:]

        return text
    """
