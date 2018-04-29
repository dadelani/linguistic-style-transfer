import argparse
import logging
import pickle
import sys

import numpy as np
import tensorflow as tf
from sklearn import metrics

from linguistic_style_transfer_model.config import global_config, model_config
from linguistic_style_transfer_model.utils import data_processor

logger = logging.getLogger(global_config.logger_name)


def get_style_transfer_score(checkpoint_dir, text_sequences_file_path, label):
    with open(global_config.classifier_vocab_save_path, 'rb') as pickle_file:
        word_index = pickle.load(pickle_file)
    with open(global_config.classifier_text_tokenizer_path, 'rb') as pickle_file:
        text_tokenizer = pickle.load(pickle_file)
    with open(global_config.classifier_vocab_size_save_path, 'rb') as pickle_file:
        vocab_size = pickle.load(pickle_file)

    with open(text_sequences_file_path) as text_file:
        actual_sequences = text_tokenizer.texts_to_sequences(text_file)
    trimmed_sequences = [
        [x if x < vocab_size else word_index[global_config.unk_token] for x in sequence]
        for sequence in actual_sequences]
    text_sequences = tf.keras.preprocessing.sequence.pad_sequences(
        trimmed_sequences, maxlen=global_config.max_sequence_length, padding='post',
        truncating='post', value=word_index[global_config.eos_token])

    x_test = np.asarray(text_sequences)
    y_test = np.asarray([label] * len(text_sequences))

    checkpoint_file = tf.train.latest_checkpoint(checkpoint_dir)
    graph = tf.Graph()
    with graph.as_default():
        gpu_options = tf.GPUOptions(allow_growth=True)
        session_conf = tf.ConfigProto(
            gpu_options=gpu_options, allow_soft_placement=True, log_device_placement=False)
        sess = tf.Session(config=session_conf)
        with sess.as_default():
            # Load the saved meta graph and restore variables
            saver = tf.train.import_meta_graph("{}.meta".format(checkpoint_file))
            saver.restore(sess, checkpoint_file)

            # Get the placeholders from the graph by name
            input_x = graph.get_operation_by_name("input_x").outputs[0]
            # input_y = graph.get_operation_by_name("input_y").outputs[0]
            dropout_keep_prob = graph.get_operation_by_name("dropout_keep_prob").outputs[0]

            # Tensors we want to evaluate
            predictions = graph.get_operation_by_name("output/predictions").outputs[0]

            # Generate batches for one epoch
            batches = data_processor.batch_iter(list(x_test), model_config.batch_size, 1, shuffle=False)

            # Collect the predictions here
            all_predictions = []

            for x_test_batch in batches:
                batch_predictions = sess.run(predictions, {input_x: x_test_batch, dropout_keep_prob: 1.0})
                all_predictions = np.concatenate([all_predictions, batch_predictions])

    # Print accuracy if y_test is defined
    if y_test is not None:
        correct_predictions = float(sum(all_predictions == y_test))
        accuracy = correct_predictions / float(len(y_test))
        # f1_score = metrics.f1_score(y_true=y_test, y_pred=all_predictions)
        confusion_matrix = metrics.confusion_matrix(y_true=y_test, y_pred=all_predictions)
        return [accuracy, confusion_matrix]

    logger.info("Nothing to evaluate")
    return 0.0


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint-dir", type=str, required=True)
    args_namespace = parser.parse_args(argv[1:])
    command_line_args = vars(args_namespace)
    checkpoint_dir = command_line_args['checkpoint_dir']

    get_style_transfer_score(checkpoint_dir, None, None)


if __name__ == '__main__':
    main(sys.argv[1:])