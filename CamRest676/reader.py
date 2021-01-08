import numpy as np
import json
import pickle
from config import global_config as cfg
from nltk.tokenize import word_tokenize
from nltk.stem import WordNetLemmatizer
from data_analysis import find_para, find_para_multiwoz
import logging
import random
import os
import re
import csv
import time, datetime


def clean_replace(s, r, t, forward=True, backward=False):
    def clean_replace_single(s, r, t, forward, backward, sidx=0):
        idx = s[sidx:].find(r)
        if idx == -1:
            return s, -1
        idx += sidx
        idx_r = idx + len(r)
        if backward:
            while idx > 0 and s[idx - 1]:
                idx -= 1
        elif idx > 0 and s[idx - 1] != ' ':
            return s, -1

        if forward:
            while idx_r < len(s) and (s[idx_r].isalpha() or s[idx_r].isdigit()):
                idx_r += 1
        elif idx_r != len(s) and (s[idx_r].isalpha() or s[idx_r].isdigit()):
            return s, -1
        return s[:idx] + t + s[idx_r:], idx_r

    sidx = 0
    while sidx != -1:
        s, sidx = clean_replace_single(s, r, t, forward, backward, sidx)
    return s


class _ReaderBase:
    class LabelSet:
        def __init__(self):
            self._idx2item = {}
            self._item2idx = {}
            self._freq_dict = {}

        def __len__(self):
            return len(self._idx2item)

        def _absolute_add_item(self, item):
            idx = len(self)
            self._idx2item[idx] = item
            self._item2idx[item] = idx

        def add_item(self, item):
            if item not in self._freq_dict:
                self._freq_dict[item] = 0
            self._freq_dict[item] += 1

        def construct(self, limit):
            l = sorted(self._freq_dict.keys(), key=lambda x: -self._freq_dict[x])
            print('Actual label size %d' % (len(l) + len(self._idx2item)))
            if len(l) + len(self._idx2item) < limit:
                logging.warning('actual label set smaller than that configured: {}/{}'
                                .format(len(l) + len(self._idx2item), limit))
            for item in l:
                if item not in self._item2idx:
                    idx = len(self._idx2item)
                    self._idx2item[idx] = item
                    self._item2idx[item] = idx
                    if len(self._idx2item) >= limit:
                        break

        def encode(self, item):
            return self._item2idx[item]

        def decode(self, idx):
            return self._idx2item[idx]

    class Vocab(LabelSet):
        def __init__(self, init=True):
            _ReaderBase.LabelSet.__init__(self)
            if init:
                self._absolute_add_item('<pad>')  # 0
                self._absolute_add_item('<go>')  # 1
                self._absolute_add_item('<unk>')  # 2
                self._absolute_add_item('<go2>')  # 3

        def load_vocab(self, vocab_path):
            f = open(vocab_path, 'rb')
            dic = pickle.load(f)
            self._idx2item = dic['idx2item']
            self._item2idx = dic['item2idx']
            self._freq_dict = dic['freq_dict']
            f.close()

        def save_vocab(self, vocab_path):
            f = open(vocab_path, 'wb')
            dic = {
                'idx2item': self._idx2item,
                'item2idx': self._item2idx,
                'freq_dict': self._freq_dict
            }
            pickle.dump(dic, f)
            f.close()

        def sentence_encode(self, word_list):
            return [self.encode(_) for _ in word_list]

        def sentence_decode(self, index_list, eos=None):
            l = [self.decode(_) for _ in index_list]
            if not eos or eos not in l:
                return ' '.join(l)
            else:
                idx = l.index(eos)
                return ' '.join(l[:idx])

        def sentence_decode_multiwoz(self, index_list, eos=None):
            l = [self.decode(_) for _ in index_list]
            if not eos or eos not in l:
                return '<cat>'.join(l)
            else:
                idx = l.index(eos)
                return '<cat>'.join(l[:idx])

        def nl_decode(self, l, eos=None):
            return [self.sentence_decode(_, eos) + '\n' for _ in l]

        def encode(self, item):
            if item in self._item2idx:
                return self._item2idx[item]
            else:
                return self._item2idx['<unk>']

        def decode(self, idx):
            if idx < len(self):
                return self._idx2item[int(idx)]
            else:
                return 'ITEM_%d' % (idx - cfg.vocab_size)

    def __init__(self):
        self.train, self.dev, self.test = [], [], []
        self.vocab = self.Vocab()
        self.result_file = ''
        self.para_result_file = ''

    def _construct(self, *args):
        """
        load data, construct vocab and store them in self.train/dev/test
        :param args:
        :return:
        """
        raise NotImplementedError('This is an abstract class, bro')

    def _bucket_by_turn(self, encoded_data):
        turn_bucket = {}
        for dial in encoded_data:
            turn_len = len(dial)
            if turn_len not in turn_bucket:
                turn_bucket[turn_len] = []
            turn_bucket[turn_len].append(dial)
        del_l = []
        for k in turn_bucket:
            if k >= 5: del_l.append(k)
            logging.debug("bucket %d instance %d" % (k, len(turn_bucket[k])))
        # for k in del_l:
        #    turn_bucket.pop(k)
        return turn_bucket

    def _mark_batch_as_supervised(self, all_batches):
        supervised_num = int(len(all_batches) * cfg.spv_proportion / 100)
        for i, batch in enumerate(all_batches):
            for dial in batch:
                for turn in dial:
                    turn['supervised'] = i < supervised_num
                    if not turn['supervised']:
                        turn['degree'] = [0.] * cfg.degree_size  # unsupervised learning. DB degree should be unknown
        return all_batches

    def _construct_mini_batch(self, data):
        all_batches = []
        batch = []
        for dial in data:
            batch.append(dial)
            if len(batch) == cfg.batch_size:
                all_batches.append(batch)
                batch = []
        # if remainder < 1/2 batch_size, just put them in the previous batch, otherwise form a new batch
        if len(batch):
            if len(batch) > 0.5 * cfg.batch_size:
                all_batches.append(batch)
            elif len(all_batches):
                all_batches[-1].extend(batch)
            else:
                all_batches.append(batch)
        return all_batches

    def _transpose_batch(self, batch):
        dial_batch = []
        turn_num = len(batch[0])
        for turn in range(turn_num):
            turn_l = {}
            for dial in batch:
                this_turn = dial[turn]
                for k in this_turn:
                    if k not in turn_l:
                        turn_l[k] = []
                    turn_l[k].append(this_turn[k])
            dial_batch.append(turn_l)
        return dial_batch

    def mini_batch_iterator(self, set_name):
        name_to_set = {'train': self.train, 'test': self.test, 'dev': self.dev}
        dial = name_to_set[set_name]
        turn_bucket = self._bucket_by_turn(dial)
        # self._shuffle_turn_bucket(turn_bucket)
        all_batches = []
        for k in turn_bucket:
            batches = self._construct_mini_batch(turn_bucket[k])
            all_batches += batches
        self._mark_batch_as_supervised(all_batches)
        random.shuffle(all_batches)
        for i, batch in enumerate(all_batches):
            yield self._transpose_batch(batch)

    def get_para_result(self, turn_batch, gen_p):
        results = []
        batch_size = len(turn_batch['user'])
        for i in range(batch_size):
            if gen_p:
                results.append(self.vocab.sentence_decode(gen_p[i], eos='EOS_U'))
            else:
                results.append('')
        return results

    def wrap_result(self, turn_batch, gen_m, gen_z, eos_syntax=None, prev_z=None):
        """
        wrap generated results
        :param gen_z:
        :param gen_m:
        :param turn_batch: dict of [i_1,i_2,...,i_b] with keys
        :return:
        """

        results = []
        if eos_syntax is None:
            eos_syntax = {'response': 'EOS_M', 'user': 'EOS_U', 'bspan': 'EOS_Z2'}
        batch_size = len(turn_batch['user'])
        for i in range(batch_size):
            entry = {}
            if prev_z is not None:
                src = prev_z[i] + turn_batch['user'][i]
            else:
                src = turn_batch['user'][i]
            for key in turn_batch:
                entry[key] = turn_batch[key][i]
                if key in eos_syntax:
                    if cfg.dataset == "multiwoz" and key == 'bspan':
                        entry[key] = self.vocab.sentence_decode_multiwoz(entry[key], eos=eos_syntax[key])
                    else:
                        entry[key] = self.vocab.sentence_decode(entry[key], eos=eos_syntax[key])
            if gen_m:
                entry['generated_response'] = self.vocab.sentence_decode(gen_m[i], eos='EOS_M')
            else:
                entry['generated_response'] = ''
            if gen_z:
                if cfg.dataset == "camrest":
                    entry['generated_bspan'] = self.vocab.sentence_decode(gen_z[i], eos='EOS_Z2')
                else:
                    entry['generated_bspan'] = self.vocab.sentence_decode_multiwoz(gen_z[i], eos='EOS_Z2')
            else:
                entry['generated_bspan'] = ''
            results.append(entry)
        write_header = False
        if not self.result_file:
            self.result_file = open(cfg.result_path, 'w')
            self.result_file.write(str(cfg))
            write_header = True
        if cfg.dataset == "camrest":
            field = ['dial_id', 'turn_num', 'user', 'generated_bspan', 'bspan', 'generated_response', 'response',
                     'u_len', 'm_len', 'supervised']
        else:
            field = ['dial_id', 'turn_num', 'user', 'generated_bspan', 'bspan', 'generated_response', 'response',
                     'u_len', 'm_len', 'supervised', 'domain']
        for result in results:
            del_k = []
            for k in result:
                if k not in field:
                    del_k.append(k)
            for k in del_k:
                result.pop(k)
        writer = csv.DictWriter(self.result_file, fieldnames=field)
        if write_header:
            self.result_file.write('START_CSV_SECTION\n')
            writer.writeheader()
        writer.writerows(results)
        return results

    def save_result_para(self, turn_batch, gen_p):
        results = []
        batch_size = len(turn_batch['user'])
        for i in range(batch_size):
            entry = {}
            entry['user'] = turn_batch['user'][i]
            entry['user'] = self.vocab.sentence_decode(entry['user'], eos='EOS_U')
            entry['generated_para'] = self.vocab.sentence_decode(gen_p[i], eos='EOS_U')
            results.append(entry)

        field = ['user', 'generated_para']
        if not self.para_result_file:
            self.para_result_file = open(cfg.para_result_path, 'w')
        writer = csv.DictWriter(self.para_result_file, fieldnames=field)
        writer.writerows(results)
        return results

    def db_search(self, constraints):
        raise NotImplementedError('This is an abstract method')

    def multi_db_search(self, constraints, domain):
        raise NotImplementedError('This is an abstract method')

    def db_degree_handler(self, z_samples, *args, **kwargs):
        """
        returns degree of database searching and it may be used to control further decoding.
        One hot vector, indicating the number of entries found: [0, 1, 2, 3, 4, >=5]
        :param z_samples: nested list of B * [T]
        :return: an one-hot control *numpy* control vector
        """
        control_vec = []

        for cons_idx_list in z_samples:
            constraints = set()
            for cons in cons_idx_list:
                if type(cons) is not str:
                    cons = self.vocab.decode(cons)
                if cons == 'EOS_Z1':
                    break
                constraints.add(cons)
            match_result = self.db_search(constraints)
            degree = len(match_result)
            # modified
            # degree = 0
            control_vec.append(self._degree_vec_mapping(degree))
        return np.array(control_vec)

    def multi_db_degree_handler(self, z_samples, z_domain):
        """
        returns degree of database searching and it may be used to control further decoding.
        One hot vector, indicating the number of entries found: [0, 1, 2, 3, 4, >=5]
        :param z_samples: nested list of B * [T]
        :return: an one-hot control *numpy* control vector
        """
        control_vec = []

        for batch_num, cons_idx_list in enumerate(z_samples):
            domain = z_domain[batch_num]
            constraints = set()
            for cons in cons_idx_list:
                if type(cons) is not str:
                    cons = self.vocab.decode(cons)
                if cons == 'EOS_Z1':
                    break
                constraints.add(cons)
            match_result = self.multi_db_search(constraints, domain)
            degree = len(match_result)
            # modified
            # degree = 0
            control_vec.append(self._degree_vec_mapping(degree))
        return np.array(control_vec)


    def _degree_vec_mapping(self, match_num):
        l = [0.] * cfg.degree_size
        l[min(cfg.degree_size - 1, match_num)] = 1.
        return l


class CamRest676Reader(_ReaderBase):
    def __init__(self):
        super().__init__()
        self._construct(cfg.data, cfg.db)
        self.result_file = ''
        self.para_result_file = ''

    def _get_tokenized_data(self, raw_data, db_data, construct_vocab):
        tokenized_data = []
        vk_map = self._value_key_map(db_data)
        for dial_id, dial in enumerate(raw_data):
            tokenized_dial = []
            for turn in dial['dial']:
                turn_num = turn['turn']
                constraint = []
                requested = []
                for slot in turn['usr']['slu']:
                    if slot['act'] == 'inform':
                        s = slot['slots'][0][1]
                        if s not in ['dontcare', 'none']:
                            constraint.extend(word_tokenize(s))
                    else:
                        requested.extend(word_tokenize(slot['slots'][0][1]))
                degree = len(self.db_search(constraint))
                requested = sorted(requested)
                constraint.append('EOS_Z1')
                requested.append('EOS_Z2')
                user = word_tokenize(turn['usr']['transcript']) + ['EOS_U']
                delex_user = turn['usr']['delex_trans'] + ' EOS_U'
                delex_user = delex_user.split(" ")
                para = word_tokenize(turn['usr']['para']) + ['EOS_U']
                delex_para = turn['usr']['delex_para'] + ' EOS_U'
                delex_para = delex_para.split(" ")
                response = word_tokenize(self._replace_entity(turn['sys']['sent'], vk_map, constraint)) + ['EOS_M']
                da = []
                for act in turn['sys']['da']:
                    if isinstance(act, list):
                        da = da + act
                    else:
                        da.append(act)
                da.append('EOS_A')
                tokenized_dial.append({
                    'dial_id': dial_id,
                    'turn_num': turn_num,
                    'user': user,
                    'delex_user': delex_user,
                    'para': para,
                    'delex_para': delex_para,
                    'response': response,
                    'dial_act': da,
                    'constraint': constraint,
                    'requested': requested,
                    'degree': degree,
                    'realize_slu': turn['usr']['slu'],
                    'replace': turn['usr']['trans_replace'],
                    'domain': '[restaurant]',
                })
                if construct_vocab:

                    for word in user + delex_user + response + constraint + requested + da:
                        self.vocab.add_item(word)
            tokenized_data.append(tokenized_dial)
        return tokenized_data

    def _replace_entity(self, response, vk_map, constraint):
        response = re.sub('[cC][., ]*[bB][., ]*\d[., ]*\d[., ]*\w[., ]*\w', 'postcode_SLOT', response)
        response = re.sub('\d{5}\s?\d{6}', 'phone_SLOT', response)
        constraint_str = ' '.join(constraint)
        for v, k in sorted(vk_map.items(), key=lambda x: -len(x[0])):
            start_idx = response.find(v)
            if start_idx == -1 \
                    or (start_idx != 0 and response[start_idx - 1] != ' ') \
                    or (v in constraint_str):
                continue
            if k not in ['name', 'address']:
                response = clean_replace(response, v, k + '_SLOT', forward=True, backward=False)
            else:
                response = clean_replace(response, v, k + '_SLOT', forward=False, backward=False)
        return response

    def _value_key_map(self, db_data):
        requestable_keys = ['address', 'name', 'phone', 'postcode', 'food', 'area', 'pricerange']
        value_key = {}
        for db_entry in db_data:
            for k, v in db_entry.items():
                if k in requestable_keys:
                    value_key[v] = k
        return value_key

    def _get_encoded_data(self, tokenized_data):
        encoded_data = []
        for dial in tokenized_data:
            encoded_dial = []
            prev_response = []
            prev_da = self.vocab.sentence_encode('EOS_A')
            for turn in dial:
                user = self.vocab.sentence_encode(turn['user'])
                delex_user = self.vocab.sentence_encode(turn['delex_user'])
                para = self.vocab.sentence_encode(turn['para'])
                delex_para = self.vocab.sentence_encode(turn['delex_para'])
                response = self.vocab.sentence_encode(turn['response'])
                dial_act = self.vocab.sentence_encode(turn['dial_act'])
                constraint = self.vocab.sentence_encode(turn['constraint'])
                requested = self.vocab.sentence_encode(turn['requested'])
                degree = self._degree_vec_mapping(turn['degree'])
                turn_num = turn['turn_num']
                dial_id = turn['dial_id']

                # final input
                encoded_dial.append({
                    'dial_id': dial_id,
                    'turn_num': turn_num,
                    'user': prev_response + user,
                    # 'delex_user': delex_user,
                    'delex_user': prev_response + delex_user,
                    'para': prev_response + para,
                    'delex_para': delex_para,
                    'pre_dial_act': prev_da,
                    'pre_response': prev_response,
                    'response': response,
                    'bspan': constraint + requested,
                    'u_len': len(prev_response + user),
                    # 'delex_u_len': len(delex_user),
                    'delex_u_len': len(prev_response + delex_user),
                    'p_len': len(prev_response + para),
                    'delex_p_len': len(delex_para),
                    'm_len': len(response),
                    'degree': degree,
                    'realize_slu': turn['realize_slu'],
                    'replace': turn['replace'],
                    'domain': turn['domain'],
                    'final_user': [],
                    'final_u_len': 0,
                })
                # modified
                prev_response = response
                prev_da = dial_act
            encoded_data.append(encoded_dial)
        return encoded_data

    def _split_data(self, encoded_data, split):
        """
        split data into train/dev/test
        :param encoded_data: list
        :param split: tuple / list
        :return:
        """
        total = sum(split)
        dev_thr = len(encoded_data) * split[0] // total
        test_thr = len(encoded_data) * (split[0] + split[1]) // total
        train, dev, test = encoded_data[:dev_thr], encoded_data[dev_thr:test_thr], encoded_data[test_thr:]
        return train, dev, test

    def _replace_para(self, raw_data, para_data_file, split, diversity_threshold, bleu_threshold):
        train, dev, test = self._split_data(raw_data, split)
        new_train = find_para(train, para_data_file, diversity_threshold, bleu_threshold)
        return new_train

    def _get_tokenized_para(self, raw_data):
        tokenized_para = []
        for dial_id, dial in enumerate(raw_data):
            tokenized_dial = []
            for turn in dial['dial']:
                para = word_tokenize(turn['usr']['para']) + ['EOS_U']
                delex_para = turn['usr']['delex_para'] + ' EOS_U'
                delex_para = delex_para.split(" ")
                tokenized_dial.append({
                    'para': para,
                    'delex_para': delex_para,
                })
            tokenized_para.append(tokenized_dial)
        return tokenized_para

    def _get_encoded_para(self, tokenized_para):
        encoded_para = []
        for dial in tokenized_para:
            encoded_dial = []
            for turn in dial:
                para = self.vocab.sentence_encode(turn['para'])
                delex_para = self.vocab.sentence_encode(turn['delex_para'])
                encoded_dial.append({
                    'para': para,
                    'delex_para': delex_para,
                    'p_len': len(para),
                    'delex_p_len': len(delex_para),
                })
            encoded_para.append(encoded_dial)
        return encoded_para

    def _construct(self, data_json_path, db_json_path):
        """
        construct encoded train, dev, test set.
        :param data_json_path:
        :param db_json_path:
        :return:
        """
        construct_vocab = False
        if not os.path.isfile(cfg.vocab_path):
            construct_vocab = True
            print('Constructing vocab file...')
        raw_data_json = open(data_json_path)
        raw_data = json.loads(raw_data_json.read().lower())
        db_json = open(db_json_path)
        db_data = json.loads(db_json.read().lower())
        self.db = db_data
        tokenized_data = self._get_tokenized_data(raw_data, db_data, construct_vocab)
        if construct_vocab:
            self.vocab.construct(cfg.vocab_size)
            self.vocab.save_vocab(cfg.vocab_path)
        else:
            self.vocab.load_vocab(cfg.vocab_path)
        encoded_data = self._get_encoded_data(tokenized_data)
        self.train, self.dev, self.test = self._split_data(encoded_data, cfg.split)
        random.shuffle(self.train)
        random.shuffle(self.dev)
        random.shuffle(self.test)
        raw_data_json.close()
        db_json.close()
    '''
    def reconstruct(self, data_json_path, db_para_path):
        raw_data_json = open(data_json_path)
        raw_data = json.loads(raw_data_json.read().lower())
        new_train_data = self._replace_para(raw_data, db_para_path, cfg.split,
                                            cfg.diversity_threshold, cfg.bleu_threshold)
        tokenized_para = self._get_tokenized_para(new_train_data)
        encoded_para = self._get_encoded_para(tokenized_para)
        for dial_num, dial in enumerate(self.train):
            for turn_num, turn in enumerate(dial):
                dial_id = turn['dial_id']
                turn_id = turn['turn_num']
                pre_sys = self.train[dial_num][turn_num]['pre_response']
                self.train[dial_num][turn_num]['para'] = pre_sys + encoded_para[dial_id][turn_id]['para']
                self.train[dial_num][turn_num]['delex_para'] = encoded_para[dial_id][turn_id]['delex_para']
                self.train[dial_num][turn_num]['p_len'] = len(pre_sys + encoded_para[dial_id][turn_id]['para'])
                self.train[dial_num][turn_num]['delex_p_len'] = encoded_para[dial_id][turn_id]['delex_p_len']
    '''

    def db_search(self, constraints):
        match_results = []
        for entry in self.db:
            entry_values = ' '.join(entry.values())
            match = True
            for c in constraints:
                if c not in entry_values:
                    match = False
                    break
            if match:
                match_results.append(entry)
        return match_results


class MultiWOZReader(_ReaderBase):
    def __init__(self):
        super().__init__()
        self._construct(cfg.data, cfg.db)
        self.result_file = ''
        self.para_result_file = ''

    def _get_tokenized_data(self, raw_data, construct_vocab):
        tokenized_data = []
        for dial_id, dial in enumerate(raw_data):
            tokenized_dial = []
            for turn in dial['log']:
                turn_num = turn['turn_num']
                constraints = []
                constraint = turn['constraint'].split(" ")
                cons_delex = turn['cons_delex'].split(" ")
                cons_entity = []
                for word in constraint:
                    if word in cons_delex:
                        if word:
                            if word[0] == "[":
                                constraints.append(word)
                        if cons_entity:
                            constraints.append(" ".join(cons_entity))
                            cons_entity = []
                    else:
                        cons_entity.append(word)
                if cons_entity:
                    constraints.append(" ".join(cons_entity))
                requested = []
                domain = turn['turn_domain']
                degree = len(self.multi_db_search(constraints, domain))
                requested = sorted(requested)
                constraints.append('EOS_Z1')
                requested.append('EOS_Z2')
                user = word_tokenize(turn['user']) + ['EOS_U']
                delex_user = turn['user_delex'] + ' EOS_U'
                delex_user = delex_user.split(" ")
                para = word_tokenize(turn['para']) + ['EOS_U']
                delex_para = turn['para_delex'] + ' EOS_U'
                delex_para = delex_para.split(" ")
                response = turn['resp'].split(" ") + ['EOS_M']
                da = turn['sys_act'].split(" ") + ['EOS_A']
                tokenized_dial.append({
                    'dial_id': dial_id,
                    'turn_num': turn_num,
                    'user': user,
                    'delex_user': delex_user,
                    'para': para,
                    'delex_para': delex_para,
                    'response': response,
                    'dial_act': da,
                    'constraint': constraints,
                    'requested': requested,
                    'degree': degree,
                    'domain': domain,
                    'realize_slu': turn['context']
                })
                if construct_vocab:
                    for word in user + response + constraints + requested + da:
                        self.vocab.add_item(word)
            tokenized_data.append(tokenized_dial)
        return tokenized_data

    def _replace_entity(self, response, vk_map, constraint):
        response = re.sub('[cC][., ]*[bB][., ]*\d[., ]*\d[., ]*\w[., ]*\w', 'postcode_SLOT', response)
        response = re.sub('\d{5}\s?\d{6}', 'phone_SLOT', response)
        constraint_str = ' '.join(constraint)
        for v, k in sorted(vk_map.items(), key=lambda x: -len(x[0])):
            start_idx = response.find(v)
            if start_idx == -1 \
                    or (start_idx != 0 and response[start_idx - 1] != ' ') \
                    or (v in constraint_str):
                continue
            if k not in ['name', 'address']:
                response = clean_replace(response, v, k + '_SLOT', forward=True, backward=False)
            else:
                response = clean_replace(response, v, k + '_SLOT', forward=False, backward=False)
        return response

    def _value_key_map(self, db_data):
        requestable_keys = ['address', 'name', 'phone', 'postcode', 'food', 'area', 'pricerange']
        value_key = {}
        for db_entry in db_data:
            for k, v in db_entry.items():
                if k in requestable_keys:
                    value_key[v] = k
        return value_key

    def _get_encoded_data(self, tokenized_data):
        encoded_data = []
        for dial in tokenized_data:
            encoded_dial = []
            prev_response = []
            prev_da = self.vocab.sentence_encode('EOS_A')
            for turn in dial:
                user = self.vocab.sentence_encode(turn['user'])
                delex_user = self.vocab.sentence_encode(turn['delex_user'])
                para = self.vocab.sentence_encode(turn['para'])
                delex_para = self.vocab.sentence_encode(turn['delex_para'])
                response = self.vocab.sentence_encode(turn['response'])
                constraint = self.vocab.sentence_encode(turn['constraint'])
                requested = self.vocab.sentence_encode(turn['requested'])
                da = self.vocab.sentence_encode(turn['dial_act'])
                degree = self._degree_vec_mapping(turn['degree'])
                turn_num = turn['turn_num']
                dial_id = turn['dial_id']
                domain = turn['domain']

                # final input
                encoded_dial.append({
                    'dial_id': dial_id,
                    'turn_num': turn_num,
                    'user': prev_response + user,
                    'delex_user': prev_response + delex_user,
                    'para': prev_response + para,
                    'delex_para': delex_para,
                    'pre_dial_act': prev_da,
                    'pre_response': prev_response,
                    'response': response,
                    'bspan': constraint + requested,
                    'u_len': len(prev_response + user),
                    'delex_u_len': len(prev_response + delex_user),
                    'p_len': len(prev_response + para),
                    'delex_p_len': len(delex_para),
                    'm_len': len(response),
                    'degree': degree,
                    'realize_slu': turn['realize_slu'],
                    'domain': domain,
                    'final_user': [],
                    'final_u_len': 0,
                })
                # modified
                prev_response = response
                prev_da = da
            encoded_data.append(encoded_dial)
        return encoded_data

    def _split_data(self, encoded_data, split):
        """
        split data into train/dev/test
        :param encoded_data: list
        :param split: tuple / list
        :return:
        """
        total = sum(split)
        dev_thr = len(encoded_data) * split[0] // total
        test_thr = len(encoded_data) * (split[0] + split[1]) // total
        train, dev, test = encoded_data[:dev_thr], encoded_data[dev_thr:test_thr], encoded_data[test_thr:]
        return train, dev, test

    def _replace_para(self, raw_data, para_data_file, split, diversity_threshold, bleu_threshold):
        train, dev, test = self._split_data(raw_data, split)
        new_train = find_para_multiwoz(train, para_data_file, diversity_threshold, bleu_threshold)
        return new_train

    def _get_tokenized_para(self, raw_data):
        tokenized_para = []
        for dial_id, dial in enumerate(raw_data):
            tokenized_dial = []
            for turn in dial['log']:
                para = word_tokenize(turn['para']) + ['EOS_U']
                delex_para = turn['para_delex'] + ' EOS_U'
                delex_para = delex_para.split(" ")
                tokenized_dial.append({
                    'para': para,
                    'delex_para': delex_para,
                })
            tokenized_para.append(tokenized_dial)
        return tokenized_para

    def _get_encoded_para(self, tokenized_para):
        encoded_para = []
        for dial in tokenized_para:
            encoded_dial = []
            for turn in dial:
                para = self.vocab.sentence_encode(turn['para'])
                delex_para = self.vocab.sentence_encode(turn['delex_para'])
                encoded_dial.append({
                    'para': para,
                    'delex_para': delex_para,
                    'p_len': len(para),
                    'delex_p_len': len(delex_para),
                })
            encoded_para.append(encoded_dial)
        return encoded_para

    def _construct(self, data_json_path, db_json_path):
        """
        construct encoded train, dev, test set.
        :param data_json_path:
        :param db_json_path:
        :return:
        """
        construct_vocab = False
        if not os.path.isfile(cfg.vocab_path):
            construct_vocab = True
            print('Constructing vocab file...')
        raw_data_json = open(data_json_path)
        raw_data = json.loads(raw_data_json.read().lower())
        db_data = {}
        with open(db_json_path + "attraction_db.json") as db_json_attraction:
            db_data["[attraction]"] = json.loads(db_json_attraction.read().lower())
        with open(db_json_path + "hospital_db.json") as db_json_hospital:
            db_data["[hospital]"] = json.loads(db_json_hospital.read().lower())
        with open(db_json_path + "hotel_db.json") as db_json_hotel:
            db_data["[hotel]"] = json.loads(db_json_hotel.read().lower())
        with open(db_json_path + "police_db.json") as db_json_police:
            db_data["[police]"] = json.loads(db_json_police.read().lower())
        with open(db_json_path + "restaurant_db.json") as db_json_restaurant:
            db_data["[restaurant]"] = json.loads(db_json_restaurant.read().lower())
        with open(db_json_path + "taxi_db.json") as db_json_taxi:
            db_data["[taxi]"] = json.loads(db_json_taxi.read().lower())
        with open(db_json_path + "train_db.json") as db_json_train:
            db_data["[train]"] = json.loads(db_json_train.read().lower())
        with open(db_json_path + "bus_db.json") as db_json_bus:
            db_data["[bus]"] = json.loads(db_json_bus.read().lower())
        self.db = db_data
        tokenized_data = self._get_tokenized_data(raw_data, construct_vocab)
        if construct_vocab:
            self.vocab.construct(cfg.vocab_size)
            self.vocab.save_vocab(cfg.vocab_path)
        else:
            self.vocab.load_vocab(cfg.vocab_path)
        encoded_data = self._get_encoded_data(tokenized_data)
        self.train, self.dev, self.test = self._split_data(encoded_data, cfg.split)
        random.shuffle(self.train)
        random.shuffle(self.dev)
        random.shuffle(self.test)
        raw_data_json.close()
    '''
    def reconstruct(self, data_json_path, db_para_path):
        raw_data_json = open(data_json_path)
        raw_data = json.loads(raw_data_json.read().lower())
        new_train_data = self._replace_para(raw_data, db_para_path, cfg.split,
                                            cfg.diversity_threshold, cfg.bleu_threshold)
        tokenized_para = self._get_tokenized_para(new_train_data)
        encoded_para = self._get_encoded_para(tokenized_para)
        for dial_num, dial in enumerate(self.train):
            for turn_num, turn in enumerate(dial):
                dial_id = turn['dial_id']
                turn_id = turn['turn_num']
                pre_sys = self.train[dial_num][turn_num]['pre_response']
                self.train[dial_num][turn_num]['para'] = pre_sys + encoded_para[dial_id][turn_id]['para']
                self.train[dial_num][turn_num]['delex_para'] = encoded_para[dial_id][turn_id]['delex_para']
                self.train[dial_num][turn_num]['p_len'] = len(pre_sys + encoded_para[dial_id][turn_id]['para'])
                self.train[dial_num][turn_num]['delex_p_len'] = encoded_para[dial_id][turn_id]['delex_p_len']

    '''

    def multi_db_search(self, constraints, domain):
        match_results = []
        domain_list = ["[attraction]", "[hospital]", "[hotel]", "[police]", "[restaurant]", "[taxi]", "[train]", "[bus]"]
        for dom in domain.split(" "):
            if dom in domain_list:
                for entry in self.db[dom]:
                    entry_values = ' '.join(entry.values())
                    match = True
                    domain_constraints = []
                    add = 0
                    for c in constraints:
                        if add and c[0] != "[":
                            domain_constraints.append(c)
                        if c[0] == "[":
                            if c == domain:
                                add = 1
                            else:
                                add = 0
                    for c in domain_constraints:
                        if c not in entry_values:
                            match = False
                            break
                    if match:
                        match_results.append(entry)
        return match_results


def pad_sequences(sequences, maxlen=None, dtype='int32',
                  padding='pre', truncating='pre', value=0.):
    if not hasattr(sequences, '__len__'):
        raise ValueError('`sequences` must be iterable.')
    lengths = []
    for x in sequences:
        if not hasattr(x, '__len__'):
            raise ValueError('`sequences` must be a list of iterables. '
                             'Found non-iterable: ' + str(x))
        lengths.append(len(x))

    num_samples = len(sequences)
    seq_maxlen = np.max(lengths)
    if maxlen is not None and cfg.truncated:
        maxlen = min(seq_maxlen, maxlen)
    else:
        maxlen = seq_maxlen
    # take the sample shape from the first non empty sequence
    # checking for consistency in the main loop below.
    sample_shape = tuple()
    for s in sequences:
        if len(s) > 0:
            sample_shape = np.asarray(s).shape[1:]
            break

    x = (np.ones((num_samples, maxlen) + sample_shape) * value).astype(dtype)
    for idx, s in enumerate(sequences):
        if not len(s):
            continue  # empty list/array was found
        if truncating == 'pre':
            trunc = s[-maxlen:]
        elif truncating == 'post':
            trunc = s[:maxlen]
        else:
            raise ValueError('Truncating type "%s" not understood' % truncating)

        # check `trunc` has expected shape
        trunc = np.asarray(trunc, dtype=dtype)
        if trunc.shape[1:] != sample_shape:
            raise ValueError('Shape of sample %s of sequence at position %s is different from expected shape %s' %
                             (trunc.shape[1:], idx, sample_shape))

        if padding == 'post':
            x[idx, :len(trunc)] = trunc
        elif padding == 'pre':
            x[idx, -len(trunc):] = trunc
        else:
            raise ValueError('Padding type "%s" not understood' % padding)
    return x


def get_glove_matrix(vocab, initial_embedding_np):
    """
    return a glove embedding matrix
    :param self:
    :param glove_file:
    :param initial_embedding_np:
    :return: np array of [V,E]
    """
    ef = open(cfg.glove_path, 'r')
    cnt = 0
    vec_array = initial_embedding_np
    old_avg = np.average(vec_array)
    old_std = np.std(vec_array)
    vec_array = vec_array.astype(np.float32)
    new_avg, new_std = 0, 0

    for line in ef.readlines():
        line = line.strip().split(' ')
        word, vec = line[0], line[1:]
        vec = np.array(vec, np.float32)
        word_idx = vocab.encode(word)
        if word.lower() in ['unk', '<unk>'] or word_idx != vocab.encode('<unk>'):
            cnt += 1
            vec_array[word_idx] = vec
            new_avg += np.average(vec)
            new_std += np.std(vec)
    new_avg /= cnt
    new_std /= cnt
    ef.close()
    logging.info('%d known embedding. old mean: %f new mean %f, old std %f new std %f' % (cnt, old_avg,
                                                                                          new_avg, old_std, new_std))
    return vec_array

