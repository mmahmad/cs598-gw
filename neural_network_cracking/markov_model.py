# author: William Melicher
import argparse
import collections
import json
import sys

import numpy as np
import pwd_guess as pg
import logging
import subprocess
import sys
from collections import defaultdict
import time
import statistics

PASSWORD_START = '\t'

DEFAULT_CONFIG = {
    'additive_smoothing_amount' : 0,
    'backoff_smoothing_threshold' : 10
}

class NoSmoothingSmoother(object):
    def __init__(self, freq_dict, config):
        self.alphabet = sorted(config.char_bag)
        self.freq_dict = freq_dict
        self.config = config

    def predict(self, ctx_arg, answer):
        assert answer.shape == (len(self.alphabet),)
        return self._predict(ctx_arg, answer)

    def freq(self, ngram):
        return self.freq_dict[ngram] if ngram in self.freq_dict else 0

    def sum_elems(self, ctx_arg, answer):
        total_sum = 0
        for i, next_char in enumerate(self.alphabet):
            ngram = ctx_arg + next_char
            freq = self.freq(ngram)
            answer[i] += freq
            total_sum += freq
        return total_sum

    def _predict(self, ctx_arg, answer):
        total_sum = self.sum_elems(ctx_arg, answer)
        for i in range(len(self.alphabet)):
            answer[i] /= total_sum

class AdditiveSmoothingSmoother(NoSmoothingSmoother):
    def __init__(self, freq_dict, config):
        super().__init__(freq_dict, config)
        self.amount = self.config.additive_smoothing_amount

    def freq(self, ngram):
        return (self.freq_dict[ngram] + self.amount
                if ngram in self.freq_dict else self.amount)

class BackoffSmoother(NoSmoothingSmoother):
    def __init__(self, freq_dict, config):
        super().__init__(freq_dict, config)
        self.threshold = self.config.backoff_smoothing_threshold
        self.amount = self.config.additive_smoothing_amount

    def freq(self, ngram):
        answer = (self.freq_dict[ngram] + self.amount
                  if ngram in self.freq_dict else self.amount)
        if answer < self.threshold:
            return 0
        return answer

    def _predict(self, ctx_arg, answer):
        total_sum = self.sum_elems(ctx_arg, answer)
        if total_sum == 0:
            answer.fill(0)
            assert len(ctx_arg) != 0, 'Backing off on 0 character string!?!'
            self.predict(ctx_arg[1:], answer)
            return
        for i in range(len(self.alphabet)):
            answer[i] /= total_sum

class MarkovModel(object):
    LOGGING_FREQUENCY = 1000000

    SMOOTHING_MAP = {
        'none' : NoSmoothingSmoother,
        'additive' : AdditiveSmoothingSmoother,
        'backoff' : BackoffSmoother
    }

    def __init__(self, config, smoothing='none', order=2):
        self.alphabet = sorted(config.char_bag)
        self.chars_to_index = dict([
            (c, i) for i, c in enumerate(self.alphabet)])
        self.smoothing = smoothing
        self.freq_dict = collections.defaultdict(int)
        self.order = order
        self.config = config
        self.smoother = None
        assert pg.PASSWORD_END in self.alphabet

    def make_smoother(self):
        return self.SMOOTHING_MAP[self.smoothing](self.freq_dict, self.config)

    def train_on_pwd(self, pwd, freq):
        pwd_len_plus_one = len(pwd) + 1
        for j in range(1, min(self.order, pwd_len_plus_one)):
            self.increment(pwd[:j], freq)
        for i in range(pwd_len_plus_one - self.order):
            self.increment(pwd[i:i + self.order], freq)
        self.increment(pwd[-self.order + 1:] + pg.PASSWORD_END, freq)

    def train(self, pwds):
        ctr = 0
        for pwd, freq in pwds:
            ctr += 1
            if ctr % self.LOGGING_FREQUENCY == 0:
                logging.info('Training on password %d', ctr)
            self.train_on_pwd(pwd, freq)
        self.smoother = self.make_smoother()

    def increment(self, pwd, freq):
        assert freq != 0
        assert len(pwd) <= self.order
        self.freq_dict[pwd] += freq

    def truncate_context(self, context):
        if len(context) >= self.order:
            return context[-(self.order - 1):]
        return context

    def probability_next_char(self, context, nc):
        assert nc in self.chars_to_index, (
            '%s not in alphabet. Please change config file' % nc)
        probs = np.zeros((len(self.alphabet), ), dtype=np.float64)
        self.predict(context, probs)
        return probs[self.chars_to_index[nc]]

    def predict(self, context, answer):
        return self.smoother.predict(self.truncate_context(context), answer)

    def saveModel(self, fname):
        logging.info('Saving model to %s', fname)
        with open(fname, 'w') as ofile:
            json.dump(self.freq_dict, ofile)

    @classmethod
    def fromModelFile(cls, fname, config, smoothing='none', order=2):
        logging.info('Loading model from %s', fname)
        with open(fname, 'r') as ifile:
            oobj = json.load(ifile)
        answer = cls(config, smoothing=smoothing, order=order)
        answer.freq_dict = oobj
        answer.smoother = answer.make_smoother()
        return answer

class BackoffMarkovModel(MarkovModel):
    def __init__(self, config, smoothing='backoff', order=2):
        super().__init__(config, smoothing, order)
        assert smoothing == 'backoff', ('Backoff Markov Model must be created '
                                        'with backoff smoothing')
        self.alphabet += PASSWORD_START

    def train_on_pwd(self, pwd, freq):
        pwd_norm = PASSWORD_START + pwd + pg.PASSWORD_END
        pwd_len = len(pwd_norm)
        for pwd_idx in range(pwd_len):
            pwd_idx_plus_one = pwd_idx + 1
            for order_idx in range(min(self.order, pwd_len - pwd_idx)):
                self.increment(pwd_norm[
                    pwd_idx:pwd_idx_plus_one + order_idx], freq)

class MarkovModelBuilder(object):
    def __init__(self, config,
                 smoothing = 'none', order = 2, model_file = None):
        self.config = config
        self.smoothing = smoothing
        self.order = order
        self.model_file = model_file

    def build(self):
        cls = MarkovModel
        if self.smoothing == 'backoff':
            cls = BackoffMarkovModel
        if self.model_file is not None:
            return cls.fromModelFile(self.model_file, self.config,
                                     smoothing=self.smoothing, order=self.order)
        else:
            return cls(self.config, smoothing=self.smoothing, order=self.order)

class MarkovGuessingFunction(object):
    def conditional_probs_many(self, astring_list):
        answer = np.zeros((len(astring_list), 1, self.ctable.vocab_size),
                          dtype=np.float64)
        for i, astring in enumerate(astring_list):
            self.model.predict(astring, answer[i, 0])
        if self.relevel_not_matching_passwords:
            self.relevel_prediction_many(answer, astring_list)
        return answer

class MarkovGuesser(MarkovGuessingFunction, pg.Guesser):
    pass

class MarkovRandomWalkGuesser(MarkovGuessingFunction, pg.RandomWalkGuesser):
    pass

class MarkovRandomWalkDelAmico(MarkovGuessingFunction, pg.RandomWalkDelAmico):
    pass

class MarkovRandomGenerator(MarkovGuessingFunction, pg.RandomGenerator):
    pass

MARKOV_GUESSER_MAP = {
    'markov_random_walk' : MarkovRandomWalkGuesser,
    'markov_delamico_random_walk' : MarkovRandomWalkDelAmico,
    'markov_human' : MarkovGuesser,
    'markov_generate_random' : MarkovRandomGenerator,
}

def read_config(args):
    fname = args.config
    if fname is not None:
        logging.info('Reading config from %s', fname)
        answer = pg.ModelDefaults.fromFile(fname)
    else:
        logging.info('Using default config')
        # Default should be to use simulated frequency optimization
        answer = pg.ModelDefaults(guesser_class='markov_human',
                                  model_type='LSTM',
                                  simulated_frequency_optimization=True)
    for key in DEFAULT_CONFIG:
        if key not in answer.adict:
            answer.adict[key] = DEFAULT_CONFIG[key]
    if args.config_values is not None:
        for cv in args.config_values.split(';'):
            name, value = cv.split('=')
            answer.adict[name] = eval(value)
    answer.validate()
    logging.info('Using config: %s', json.dumps(answer.as_dict(), indent=4))
    return answer

def train(args):
    if args.ofile is None:
        logging.critical('Must provide --ofile argument! Exiting...')
        sys.exit(1)
    logging.info('Beginning training of %s-gram model...', args.k_order)
    config = read_config(args)
    model = MarkovModelBuilder(
        config, order = args.k_order, smoothing = args.smoothing).build()
    model.train(pg.ResetablePwdList(
        [args.train_file], [args.train_format], config).as_iterator(quick=True))
    model.saveModel(args.ofile)

def make_guesser_builder(args):
    config = read_config(args)
    if config.guesser_class not in MARKOV_GUESSER_MAP:
        logging.critical(('Configuration option guesser_class is %s must be '
                          'one of: %s'), config.guesser_class,
                         ", ".join(sorted(list(MARKOV_GUESSER_MAP.keys()))))
        sys.exit(1)
    if args.model_file is None:
        logging.critical('Must provide --model-file argument! Exiting...')
        sys.exit(1)
    if args.ofile is None:
        logging.critical('Must provide ofile argument! Exiting...')
        sys.exit(1)

    config.password_test_fname = args.password_file
    guesser_builder = pg.GuesserBuilder(config)
    guesser_builder.add_model(MarkovModelBuilder(
        config, smoothing=args.smoothing, order=args.k_order,
        model_file=args.model_file).build())
    guesser_builder.add_file(args.ofile)
    guesser_builder.other_class_builders = MARKOV_GUESSER_MAP
    return guesser_builder.build()

def main(args):
    # TODO: Comment the 2 lines below for live prediction
    # test_markov_model(args)
    test_markov_model_prefix(args)
    sys.exit(0)

    ###########################
    pg.init_logging(vars(args))
    if args.train_file is not None:
        train(args)
    elif args.model_file is not None:
    #    guesser = make_guesser_builder(args)
        guesser = make_guesser_builder(args)
        if args.password_file is None:
            """
            guesser.complete_guessing(start='passw')
            # take args.ofile, sort it and return top 5 passwords
            # sort: sort -gr -k2 -t$'\t' markov_ofile.txt -o sorted_markov_ofile.txt
            # subprocess.run(["ls", "-l"])
            subprocess.run(f"sort -gr -k2 -t$'\\t' {args.ofile} -o sorted_{args.ofile}".split(" "))
            # first 5: head -n5 sorted_markov_ofile.txt
            subprocess.run(f"head -n5 sorted_{args.ofile}".split(" "))
            """
            # take input
            # i = 0
            context_chars = ""
            while len(context_chars) < 2:
                context_chars += input("Enter context chars: ")
            
            while len(context_chars) < 20:
                # args.ofile = f'markov_ofile{i}.txt'
                # guesser = make_guesser_builder(args)
                # guess
                
                guesser.ostream = open(args.ofile, 'w') # empty file
                guesser.generated = 0
                guesser.complete_guessing(context_chars)
                    
                # take args.ofile, sort it and return top 5 passwords
                # sort: sort -gr -k2 -t$'\t' markov_ofile.txt -o sorted_markov_ofile.txt
                # subprocess.run(["ls", "-l"])
                subprocess.call(["sort", "-gr", "-k2", "-t", "\t" , args.ofile, "-o", f"sorted_{args.ofile}"])
                print("sorted.")
                # subprocess.run(f"sort -gr -k2 -t$'\t' {args.ofile} -o sorted_{args.ofile}".split(" "))
                # subprocess.run(f"sort -gr -k2 -t'\\t' {args.ofile} -o sorted_{args.ofile}".split(" "))
                # first 5: head -n5 sorted_markov_ofile.txt
                subprocess.run(f"head -n10 sorted_{args.ofile}".split(" "))
                # input("Press Enter to continue...")
                # clear the files
                #open(args.ofile, 'w').close()
                #open(f'sorted_{args.ofile}', 'w').close()

                next_context_chars = input("Enter next char (or $ to start again): ")
                if next_context_chars == '$':
                    while next_context_chars == '$':
                        next_context_chars = input('Enter beginning context char(s): ')
                        context_chars = next_context_chars
                else:
                    context_chars += next_context_chars
        else:
            guesser.calculate_probs()
    else:
        logging.error('Must provide --train-file or --model-file flag. ')

def test_markov_model(args):
    pg.init_logging(vars(args))
    if args.train_file is not None:
        train(args)
    elif args.model_file is not None:
    #    guesser = make_guesser_builder(args)
        guesser = make_guesser_builder(args)
        if args.password_file is None:

            # TESTING_FILE = '/Volumes/Samsung_T5/cs598-gw/passdata_final/testing.txt'
            # TESTING_FILE = '/Volumes/Samsung_T5/cs598-gw/passdata_final/small_testing_20.txt'
            # TESTING_FILE = '/Volumes/Samsung_T5/cs598-gw/passdata_final/testing.txt'
            # TESTING_FILE = '/Volumes/Samsung_T5/cs598-gw/passdata_final/testing_145_plus.txt'
            TESTING_FILE = '/Volumes/Samsung_T5/cs598-gw/passdata_final/testing_120_plus.txt'
            PASSWORDS_TO_TEST= 880 # 120 already tested.
            incorrect_predictions = 0 # keeps track of how many passwords next_char could not be predicted (regardless of length of prefix)
            next_char_prediction_time = defaultdict(list)

            with open(TESTING_FILE) as tf:
                for test_pwd_count, testing_pwd in enumerate(tf):
                    if test_pwd_count+1 > PASSWORDS_TO_TEST:
                        print(f"{PASSWORDS_TO_TEST} passwords tested.")
                        break
                    print(f"testing pwd # {test_pwd_count+1} / {PASSWORDS_TO_TEST}: {testing_pwd}")
                    
                    pwd = testing_pwd.strip() # to remove \n
                    # create prefixes
                    all_prefixes = [(pwd[:i], pwd[i]) for i in range(len(pwd))]
                    # print(f"for pwd: {pwd} -> all_prefixes: {all_prefixes}")

                    """
                    Start with the first 2 context chars instead of starting with empty or 1 char.
                    """
                    all_prefixes.pop(0) # first char (empty)
                    all_prefixes.pop(0) # second char (1st char). First prefix in all_prefixes now has 2 chars
                    """
                    For each prefix, compute the next char.
                    """
                    
                    for prefix, next_char in all_prefixes:
                        start_time = 0
                        is_prediction_correct = False
                        context_chars = prefix
                        try:
                            # guess the next chars
                            guesser.ostream = open(args.ofile, 'w') # empty file
                            guesser.generated = 0
                            
                            start = time.process_time()
                            guesser.complete_guessing(context_chars)
                            

                        except Exception as e:
                            print(f"Unable to predict pwd: {pwd}")
                            incorrect_predictions += 1
                            break

                        # sort the passwords by probability (descending)
                        subprocess.call(["sort", "-gr", "-k2", "-t", "\t" , args.ofile, "-o", f"sorted_{args.ofile}"])

                        # read the first 10 most likely guesses
                        K_MOST_LIKELY = 10
                        with open(f"sorted_{args.ofile}") as myfile:
                            # most_likely_pwds_for_prefix = [next(myfile).strip().split('\t')[0] for x in range(K_MOST_LIKELY)]
                            # most_likely_pwds_for_prefix = [next(myfile).split("\t")[0].strip() for x in range(K_MOST_LIKELY)]
                            most_likely_pwds_for_prefix = [line.split("\t")[0].strip() for index, line in enumerate(myfile) if index < 10]
                        # print(most_likely_pwds_for_prefix)

                        most_likely_next_chars = set([p[len(context_chars)] for p in most_likely_pwds_for_prefix])
                        next_char_prediction_time[len(prefix)].append(time.process_time() - start)

                        # print(f"most_likely_next_chars set: {most_likely_next_chars}")
                        if next_char in most_likely_next_chars:
                            # yay! correct prediction, so move to next prefix
                            is_prediction_correct = True
                            
                            # break
                            continue
                        else:
                            # next chars don't have correct char, so break
                            print("unable to predict next char. Moving to next password")
                            incorrect_predictions += 1
                            break

                        # if is_prediction_correct == True:
                        #     # move to next prefix
                        #     continue      
                        # else:
                        #     print("unable to predict next char. Moving to next password")
                        #     incorrect_predictions += 1
                        #     break     
                    print(f"Pwd {testing_pwd} tested. Incorrect predictions: {incorrect_predictions} / {test_pwd_count+1}")
                    
                    print("Progress: {0:.0%}".format((test_pwd_count+1)/PASSWORDS_TO_TEST))                
            # we've tested all passwords. Return # of incorrect predictions
            print(f"Testing complete. Incorrect predictions: {incorrect_predictions} / {test_pwd_count}")

            """
            Calculate mean and median CPU times to predict next char for each
            prefix length (2-)
            """
            mean_times = defaultdict(float)
            median_times = defaultdict(float)
            # print median and mean of next_char_prediction_time
            for prefix_length, times in next_char_prediction_time.items():
                mean_times[prefix_length] = round(statistics.mean(times), 2)
                median_times[prefix_length] = round(statistics.median(times), 2)

            print("Next char prediction for every prefix length (2-) MEAN times:")
            print(mean_times)

            print("Next char prediction for every prefix length (2-) MEDIAN times:")
            print(median_times)

            print("Done.")
            
                        
        else:
            guesser.calculate_probs()
    else:
        logging.error('Must provide --train-file or --model-file flag. ')
def test_markov_model_prefix(args):
    pg.init_logging(vars(args))
    if args.train_file is not None:
        train(args)
    elif args.model_file is not None:
    #    guesser = make_guesser_builder(args)
        guesser = make_guesser_builder(args)
        if args.password_file is None:


            TESTING_FILE = '/projects/eng/shared/CS598GW-FA19/mmahmad3/data/4.txt'
            PASSWORDS_TO_TEST= 33324
            incorrect_predictions = 0 # keeps track of how many passwords next_char could not be predicted (regardless of length of prefix)
            next_char_prediction_time = defaultdict(list)

            with open(TESTING_FILE) as tf:
                for test_pwd_count, testing_pwd in enumerate(tf):
                    if test_pwd_count+1 > PASSWORDS_TO_TEST:
                        print(f"{PASSWORDS_TO_TEST} passwords tested.")
                        break
                    print(f"testing pwd # {test_pwd_count+1} / {PASSWORDS_TO_TEST}: {testing_pwd}")

                    # split based on |
                    pwd = testing_pwd.strip()
                    tokens = testing_pwd.split("|")
                    prefix = tokens[0]
                    all_possible_next_chars_ground_truth = tokens[1]

                    context_chars = prefix
                    try:
                        # guess the next chars
                        guesser.ostream = open(args.ofile, 'w') # empty file
                        guesser.generated = 0
                        
                        guesser.complete_guessing(context_chars)
                    except Exception as e:
                        pass
                        # print(e)
                        # print(f"no predictions for: {pwd}. Prefix: {prefix}")
                        # incorrect_predictions += 1
                        # print(f"Pwd {testing_pwd} tested. Incorrect predictions: {incorrect_predictions} / {test_pwd_count+1}")
                        # print("Progress: {0:.0%}".format((test_pwd_count+1)/PASSWORDS_TO_TEST))
                        # continue


                    # sort the passwords by probability (descending)
                    subprocess.call(["sort", "-gr", "-k2", "-t", "\t" , args.ofile, "-o", f"sorted_{args.ofile}"])

                    # read the first 10 most likely guesses
                    K_MOST_LIKELY = 10
                    most_likely_10_next_chars_set = set()
                    with open(f"sorted_{args.ofile}") as myfile:
                        for line in myfile:
                            if len(most_likely_10_next_chars_set) < K_MOST_LIKELY:
                                p = line
                                # get the next char and add to set
                                most_likely_10_next_chars_set.add(p.split("\t")[0].strip()[len(context_chars)])

                    # if no predictions available, incorrect_predictions += 1 then continue to next prefix
                    if len(most_likely_10_next_chars_set) == 0:
                        print(f"no predictions for {pwd}. Prefix: {prefix}")
                        incorrect_predictions += 1
                        print(f"Pwd {testing_pwd} tested. Incorrect predictions: {incorrect_predictions} / {test_pwd_count+1}")
                        print("Progress: {0:.0%}".format((test_pwd_count+1)/PASSWORDS_TO_TEST))
                        continue
                    else:
                        # check if any one of them is in the ground truth
                        if bool(set(all_possible_next_chars_ground_truth) & most_likely_10_next_chars_set):
                            # ground truth has a password with our prefix and the next char we predicted
                            pass
                        else:
                            incorrect_predictions += 1

                    print(f"Pwd {testing_pwd} tested. Incorrect predictions: {incorrect_predictions} / {test_pwd_count+1}")
                    print("Progress: {0:.0%}".format((test_pwd_count+1)/PASSWORDS_TO_TEST))
            
            # Testing file closed. Done with all testing
            print(f"Testing complete. Incorrect predictions: {incorrect_predictions} / {test_pwd_count}")
            print("Done.")
            
        else:
            guesser.calculate_probs()
    else:
        logging.error('Must provide --train-file or --model-file flag. ')
if __name__=='__main__':
    parser = argparse.ArgumentParser(
        description='Train and guess with a markov model. ')
    parser.add_argument('-t', '--train-file',
                        help='Training file. Will train a model. ')
    parser.add_argument('-o', '--ofile', help='Output file. ')
    parser.add_argument('-m', '--model-file',
                        help='Model file. Will guess passwords. ')
    parser.add_argument('-p', '--password-file',
                        help='Password file. Will calculate probabilities. ')
    parser.add_argument('-k', '--k-order', type=int, default=2,
                        help=('Giving an argument of 2 means using 1 '
                              'character of context to predict the next '
                              'character. Default is 2. '))
    parser.add_argument('-c', '--config', help='Config file. ')
    parser.add_argument('-s', '--smoothing', default = 'none',
                        help='Type of smoothing. Default is no smoothing. ',
                        choices=sorted(MarkovModel.SMOOTHING_MAP.keys()))
    parser.add_argument('-f', '--train-format',
                        help='Can be list or tsv. Default is tsv',
                        choices=['list', 'tsv'], default='tsv')
    parser.add_argument('--cv', '--config-values', dest='config_values',
                        help=('Provide configuration values in format: '
                              'NAME=VALUE;NAME2=VALUE'))
    parser.add_argument('-l', '--log-file')
    parser.add_argument('--log-level', default='info', choices=pg.log_level_map)
    main(parser.parse_args())
