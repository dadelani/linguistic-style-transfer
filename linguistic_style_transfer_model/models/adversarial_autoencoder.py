import logging
import pickle
from datetime import datetime as dt

import numpy as np
import tensorflow as tf

from linguistic_style_transfer_model.config import global_config, model_config

logger = logging.getLogger(global_config.logger_name)


class AdversarialAutoencoder:

    def __init__(self, padded_sequences, text_sequence_lengths, one_hot_labels, num_labels,
                 word_index, encoder_embedding_matrix, decoder_embedding_matrix, bow_representations):
        self.padded_sequences = padded_sequences
        self.text_sequence_lengths = text_sequence_lengths
        self.one_hot_labels = one_hot_labels
        self.num_labels = num_labels
        self.word_index = word_index
        self.encoder_embedding_matrix = encoder_embedding_matrix
        self.decoder_embedding_matrix = decoder_embedding_matrix
        self.bow_representations = bow_representations

    def get_sentence_embedding(self, encoder_embedded_sequence):
        scope_name = "sentence_embedding"
        with tf.name_scope(scope_name):
            encoder_cell_fw = tf.nn.rnn_cell.DropoutWrapper(
                cell=tf.contrib.rnn.GRUCell(num_units=model_config.encoder_rnn_size),
                input_keep_prob=model_config.recurrent_state_keep_prob,
                output_keep_prob=model_config.recurrent_state_keep_prob,
                state_keep_prob=model_config.recurrent_state_keep_prob)
            encoder_cell_bw = tf.nn.rnn_cell.DropoutWrapper(
                cell=tf.contrib.rnn.GRUCell(num_units=model_config.encoder_rnn_size),
                input_keep_prob=model_config.recurrent_state_keep_prob,
                output_keep_prob=model_config.recurrent_state_keep_prob,
                state_keep_prob=model_config.recurrent_state_keep_prob)

            _, encoder_states = tf.nn.bidirectional_dynamic_rnn(
                cell_fw=encoder_cell_fw,
                cell_bw=encoder_cell_bw,
                inputs=encoder_embedded_sequence,
                scope=scope_name,
                sequence_length=self.sequence_lengths,
                dtype=tf.float32)

            return tf.concat(values=encoder_states, axis=1)

    def get_style_embedding(self, sentence_embedding):

        style_embedding = tf.nn.dropout(
            x=tf.layers.dense(
                inputs=sentence_embedding, units=model_config.style_embedding_size,
                activation=tf.nn.leaky_relu, name="style_embedding"),
            keep_prob=model_config.fully_connected_keep_prob)

        return style_embedding

    def get_content_embedding(self, sentence_embedding):

        content_embedding = tf.nn.dropout(
            x=tf.layers.dense(
                inputs=sentence_embedding, units=model_config.content_embedding_size,
                activation=tf.nn.leaky_relu, name="content_embedding"),
            keep_prob=model_config.fully_connected_keep_prob)

        return content_embedding

    def gaussian_noise_layer(self, input_layer, std):
        noise = tf.random_normal(shape=tf.shape(input_layer), mean=0.0, stddev=std, dtype=tf.float32)
        return input_layer + noise

    def get_style_label_and_bow_prediction(self, style_embedding):

        style_label_mlp = tf.nn.dropout(
            x=tf.layers.dense(
                inputs=style_embedding, units=model_config.style_embedding_size,
                activation=tf.nn.leaky_relu, name="style_label_prediction_dense"),
            keep_prob=model_config.fully_connected_keep_prob)

        style_label_prediction = tf.layers.dense(
            inputs=style_label_mlp, units=self.num_labels,
            activation=tf.nn.softmax, name="style_label_prediction")

        bow_prediction = tf.layers.dense(
            inputs=style_label_mlp, units=global_config.vocab_size, name="bow_prediction")

        return [style_label_prediction, bow_prediction]

    def get_adversarial_label_prediction(self, content_embedding):

        adversarial_label_mlp = tf.nn.dropout(
            x=tf.layers.dense(
                inputs=self.gaussian_noise_layer(
                    content_embedding, model_config.adversarial_discriminator_noise_stddev),
                units=model_config.content_embedding_size,
                activation=tf.nn.leaky_relu, name="adversarial_label_prediction_dense"),
            keep_prob=model_config.fully_connected_keep_prob)

        adversarial_label_prediction = tf.layers.dense(
            inputs=self.gaussian_noise_layer(
                adversarial_label_mlp, model_config.adversarial_discriminator_noise_stddev),
            units=self.num_labels,
            activation=tf.nn.softmax, name="adversarial_label_prediction")

        return adversarial_label_prediction

    def generate_output_sequence(self, embedded_sequence, generative_embedding, decoder_embeddings):

        decoder_cell = tf.nn.rnn_cell.DropoutWrapper(
            cell=tf.contrib.rnn.GRUCell(num_units=model_config.decoder_rnn_size),
            input_keep_prob=model_config.recurrent_state_keep_prob,
            output_keep_prob=model_config.recurrent_state_keep_prob,
            state_keep_prob=model_config.recurrent_state_keep_prob)

        projection_layer = tf.layers.Dense(units=global_config.vocab_size, use_bias=False)

        with tf.name_scope("training_decoder"):
            training_helper = tf.contrib.seq2seq.TrainingHelper(
                inputs=embedded_sequence,
                sequence_length=self.sequence_lengths)

            training_decoder = tf.contrib.seq2seq.BasicDecoder(
                cell=decoder_cell, helper=training_helper,
                initial_state=generative_embedding,
                output_layer=projection_layer)
            training_decoder.initialize("training_decoder")

            training_decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
                decoder=training_decoder, impute_finished=True,
                maximum_iterations=global_config.max_sequence_length,
                scope="training_decoder")

        with tf.name_scope("inference_decoder"):
            inference_decoder = tf.contrib.seq2seq.BeamSearchDecoder(
                cell=decoder_cell, embedding=decoder_embeddings,
                start_tokens=tf.fill(
                    dims=[model_config.batch_size],
                    value=self.word_index[global_config.sos_token]),
                end_token=self.word_index[global_config.eos_token],
                initial_state=tf.contrib.seq2seq.tile_batch(
                    t=generative_embedding, multiplier=model_config.beam_search_width),
                beam_width=model_config.beam_search_width, output_layer=projection_layer,
                length_penalty_weight=0.0
            )
            inference_decoder.initialize("inference_decoder")

            inference_decoder_output, _, final_sequence_lengths = tf.contrib.seq2seq.dynamic_decode(
                decoder=inference_decoder, impute_finished=False,
                maximum_iterations=global_config.max_sequence_length,
                scope="inference_decoder")

        return training_decoder_output.rnn_output, \
               inference_decoder_output.predicted_ids[:, :, 0], \
               final_sequence_lengths[:, 0]  # index 0 gets the best beam search outcome

    def get_reconstruction_weight(self, epoch):
        min_epoch = tf.constant(value=model_config.autoencoder_annealment_min_epoch, dtype=tf.int32)
        max_epoch = tf.constant(value=global_config.training_epochs, dtype=tf.int32)

        reconstruction_weight = tf.divide(
            x=tf.maximum(x=(epoch - min_epoch), y=tf.constant(0)),
            y=max_epoch - min_epoch)
        reconstruction_weight = tf.cast(reconstruction_weight, tf.float32)

        return reconstruction_weight

    def build_model(self):

        # model inputs
        self.input_sequence = tf.placeholder(
            dtype=tf.int32, shape=[model_config.batch_size, global_config.max_sequence_length],
            name="input_sequence")
        logger.debug("input_sequence: {}".format(self.input_sequence))

        self.input_label = tf.placeholder(
            dtype=tf.float32, shape=[model_config.batch_size, self.num_labels],
            name="input_label")
        logger.debug("input_label: {}".format(self.input_label))

        self.input_bow_representations = tf.placeholder(
            dtype=tf.float32, shape=[model_config.batch_size, global_config.vocab_size],
            name="input_bow_representations")
        logger.debug("input_bow_representations: {}".format(self.input_bow_representations))

        self.sequence_lengths = tf.placeholder(
            dtype=tf.int32, shape=[model_config.batch_size],
            name="sequence_lengths")
        logger.debug("sequence_lengths: {}".format(self.sequence_lengths))

        self.conditioned_generation_mode = tf.placeholder(
            dtype=tf.bool, name="conditioned_generation_mode")
        logger.debug("conditioned_generation_mode: {}".format(self.conditioned_generation_mode))

        self.conditioning_embedding = tf.placeholder(
            dtype=tf.float32,
            shape=[model_config.batch_size, model_config.style_embedding_size],
            name="conditioning_embedding")
        logger.debug("conditioning_embedding: {}".format(self.conditioning_embedding))

        self.epoch = tf.placeholder(dtype=tf.int32, shape=(), name="epoch")
        logger.debug("epoch: {}".format(self.epoch))

        self.reconstruction_weight = self.get_reconstruction_weight(self.epoch)
        logger.debug("reconstruction_weight: {}".format(self.reconstruction_weight))

        decoder_input = tf.concat(
            values=[
                tf.fill(
                    dims=[model_config.batch_size, 1],
                    value=self.word_index[global_config.sos_token],
                    name="start_tokens"),
                self.input_sequence],
            axis=1, name="decoder_input")

        with tf.device('/cpu:0'):
            # word embeddings matrices
            encoder_embeddings = tf.get_variable(
                initializer=self.encoder_embedding_matrix, dtype=tf.float32,
                trainable=True, name="encoder_embeddings")
            logger.debug("encoder_embeddings: {}".format(encoder_embeddings))

            decoder_embeddings = tf.get_variable(
                initializer=self.decoder_embedding_matrix, dtype=tf.float32,
                trainable=True, name="decoder_embeddings")
            logger.debug("decoder_embeddings: {}".format(decoder_embeddings))

            # embedded sequences
            encoder_embedded_sequence = tf.nn.dropout(
                x=tf.nn.embedding_lookup(
                    params=encoder_embeddings, ids=self.input_sequence),
                keep_prob=model_config.sequence_word_keep_prob,
                name="encoder_embedded_sequence")
            logger.debug("encoder_embedded_sequence: {}".format(encoder_embedded_sequence))

            decoder_embedded_sequence = tf.nn.dropout(
                x=tf.nn.embedding_lookup(params=decoder_embeddings, ids=decoder_input),
                keep_prob=model_config.sequence_word_keep_prob,
                name="decoder_embedded_sequence")
            logger.debug("decoder_embedded_sequence: {}".format(decoder_embedded_sequence))

        sentence_embedding = self.get_sentence_embedding(encoder_embedded_sequence)

        # style embedding
        self.style_embedding = tf.cond(
            pred=self.conditioned_generation_mode,
            true_fn=lambda: self.conditioning_embedding,
            false_fn=lambda: self.get_style_embedding(sentence_embedding))
        logger.debug("style_embedding: {}".format(self.style_embedding))

        # content embedding
        content_embedding = self.get_content_embedding(sentence_embedding)
        logger.debug("content_embedding: {}".format(content_embedding))

        # concatenated generative embedding
        generative_embedding = tf.layers.dense(
            inputs=tf.concat(values=[self.style_embedding, content_embedding], axis=1),
            units=model_config.decoder_rnn_size, activation=tf.nn.leaky_relu,
            name="generative_embedding")
        logger.debug("generative_embedding: {}".format(generative_embedding))

        # sequence predictions
        with tf.name_scope('sequence_prediction'):
            training_output, self.inference_output, self.final_sequence_lengths = \
                self.generate_output_sequence(
                    decoder_embedded_sequence, generative_embedding, decoder_embeddings)
            logger.debug("training_output: {}".format(training_output))
            logger.debug("inference_output: {}".format(self.inference_output))

        # adversarial loss
        with tf.name_scope('adversarial_loss'):
            adversarial_label_prediction = self.get_adversarial_label_prediction(content_embedding)
            logger.debug("adversarial_label_prediction: {}".format(adversarial_label_prediction))

            self.adversarial_entropy = tf.reduce_mean(
                input_tensor=tf.reduce_sum(
                    input_tensor=-adversarial_label_prediction *
                                 tf.log(adversarial_label_prediction),
                    axis=1))
            logger.debug("adversarial_entropy: {}".format(self.adversarial_entropy))

            self.adversarial_loss = tf.losses.softmax_cross_entropy(
                onehot_labels=self.input_label, logits=adversarial_label_prediction, label_smoothing=0.1)
            logger.debug("adversarial_loss: {}".format(self.adversarial_loss))

        # style prediction loss
        with tf.name_scope('style_prediction_loss'):
            [style_label_prediction, bow_prediction] = \
                self.get_style_label_and_bow_prediction(self.style_embedding)
            logger.debug("style_label_prediction: {}".format(style_label_prediction))
            logger.debug("bow_prediction: {}".format(bow_prediction))

            self.style_prediction_loss = tf.losses.softmax_cross_entropy(
                onehot_labels=self.input_label, logits=style_label_prediction, label_smoothing=0.1)
            logger.debug("style_prediction_loss: {}".format(self.style_prediction_loss))

            self.bow_prediction_loss = tf.reduce_mean(
                input_tensor=tf.nn.sigmoid(
                    x=tf.nn.softmax_cross_entropy_with_logits(
                        labels=self.input_bow_representations,
                        logits=bow_prediction)),
                name="bow_prediction_loss")
            logger.debug("bow_prediction_loss: {}".format(self.bow_prediction_loss))

        # reconstruction loss
        with tf.name_scope('reconstruction_loss'):
            batch_maxlen = tf.reduce_max(self.sequence_lengths)
            logger.debug("batch_maxlen: {}".format(batch_maxlen))

            # the training decoder only emits outputs equal in time-steps to the
            # max time in the current batch
            target_sequence = tf.slice(
                input_=self.input_sequence,
                begin=[0, 0],
                size=[model_config.batch_size, batch_maxlen],
                name="target_sequence")
            logger.debug("target_sequence: {}".format(target_sequence))

            output_sequence_mask = tf.sequence_mask(
                lengths=tf.add(x=self.sequence_lengths, y=1),
                maxlen=batch_maxlen,
                dtype=tf.float32)

            self.reconstruction_loss = tf.contrib.seq2seq.sequence_loss(
                logits=training_output, targets=target_sequence,
                weights=output_sequence_mask)
            logger.debug("reconstruction_loss: {}".format(self.reconstruction_loss))

        # tensorboard logging variable summaries
        tf.summary.scalar(tensor=self.reconstruction_loss, name="reconstruction_loss_summary")
        tf.summary.scalar(tensor=self.style_prediction_loss, name="style_prediction_loss_summary")
        tf.summary.scalar(tensor=self.adversarial_loss, name="adversarial_loss_summary")

    def get_batch_indices(self, offset, batch_number, data_limit):

        start_index = offset + (batch_number * model_config.batch_size)
        end_index = offset + ((batch_number + 1) * model_config.batch_size)
        end_index = data_limit if end_index > data_limit else end_index

        return start_index, end_index

    def run_batch(self, sess, start_index, end_index, fetches, shuffled_padded_sequences,
                  shuffled_one_hot_labels, shuffled_text_sequence_lengths, shuffled_bow_representations,
                  conditioning_embedding, conditioned_generation_mode, current_epoch):

        if not conditioned_generation_mode:
            conditioning_embedding = np.random.uniform(
                size=(model_config.batch_size, model_config.style_embedding_size),
                low=-0.05, high=0.05).astype(dtype=np.float32)

        ops = sess.run(
            fetches=fetches,
            feed_dict={
                self.input_sequence: shuffled_padded_sequences[start_index: end_index],
                self.input_label: shuffled_one_hot_labels[start_index: end_index],
                self.sequence_lengths: shuffled_text_sequence_lengths[start_index: end_index],
                self.input_bow_representations: shuffled_bow_representations[start_index: end_index],
                self.conditioned_generation_mode: conditioned_generation_mode,
                self.conditioning_embedding: conditioning_embedding,
                self.epoch: current_epoch
            })

        return ops

    def train(self, sess, data_size):

        writer = tf.summary.FileWriter(
            logdir="/tmp/tensorflow_logs/" + dt.now().strftime("%Y%m%d-%H%M%S") + "/",
            graph=sess.graph)

        trainable_variables = tf.trainable_variables()
        logger.debug("trainable_variables: {}".format(trainable_variables))
        self.composite_loss = \
            self.reconstruction_loss * self.reconstruction_weight \
            - (self.adversarial_entropy * model_config.adversarial_discriminator_loss_weight) \
            + (self.style_prediction_loss * model_config.style_prediction_loss_weight) \
            - (self.bow_prediction_loss * model_config.bow_prediction_loss_weight)
        tf.summary.scalar(tensor=self.composite_loss, name="composite_loss")
        self.all_summaries = tf.summary.merge_all()

        adversarial_training_optimizer = tf.train.GradientDescentOptimizer(
            learning_rate=model_config.adversarial_discriminator_learning_rate)

        # optimize adversarial classification
        adversarial_training_optimizer = tf.train.RMSPropOptimizer(
            learning_rate=model_config.adversarial_discriminator_learning_rate)
        adversarial_training_variables = [
            x for x in trainable_variables if any(
                scope in x.name for scope in adversarial_variable_labels)]
        logger.debug("adversarial_training_optimizer.variables: {}".format(adversarial_training_variables))
        adversarial_training_operation = None
        for i in range(model_config.adversarial_discriminator_iterations):
            adversarial_training_operation = adversarial_training_optimizer.minimize(
                loss=self.adversarial_loss, var_list=adversarial_training_variables)

        # optimize reconstruction
        reconstruction_training_optimizer = tf.train.AdamOptimizer(
            learning_rate=model_config.autoencoder_learning_rate)
        reconstruction_training_variables = [
            x for x in trainable_variables if all(
                scope not in x.name for scope in adversarial_variable_labels)]
        logger.debug("reconstruction_training_optimizer.variables: {}".format(reconstruction_training_variables))
        reconstruction_training_operation = None
        for i in range(model_config.autoencoder_iterations):
            reconstruction_training_operation = reconstruction_training_optimizer.minimize(
                loss=self.composite_loss, var_list=reconstruction_training_variables)

        sess.run(tf.global_variables_initializer())
        saver = tf.train.Saver()

        training_examples_size = data_size
        num_batches = training_examples_size // model_config.batch_size
        logger.debug("Training - texts shape: {}; labels shape {}"
                     .format(self.padded_sequences[:training_examples_size].shape,
                             self.one_hot_labels[:training_examples_size].shape))

        for current_epoch in range(1, global_config.training_epochs + 1):

            all_style_embeddings = list()
            shuffle_indices = np.random.permutation(np.arange(data_size))
            shuffled_padded_sequences = self.padded_sequences[shuffle_indices]
            shuffled_one_hot_labels = self.one_hot_labels[shuffle_indices]
            shuffled_text_sequence_lengths = self.text_sequence_lengths[shuffle_indices]
            shuffled_bow_representations = self.bow_representations[shuffle_indices]

            shuffle_indices = np.random.permutation(
                np.arange(start=0, stop=data_size, step=model_config.batch_size))

            for shuffle_index in shuffle_indices:
                [start_index, end_index] = [shuffle_index, shuffle_index + model_config.batch_size]

                fetches = \
                    [reconstruction_training_operation,
                     adversarial_training_operation,
                     self.reconstruction_loss,
                     self.adversarial_loss,
                     self.adversarial_entropy,
                     self.style_prediction_loss,
                     self.bow_prediction_loss,
                     self.composite_loss,
                     self.style_embedding,
                     self.all_summaries]

                [_, _, reconstruction_loss, adversarial_loss, adversarial_entropy, style_loss,
                 bow_representation_loss, composite_loss, style_embeddings, all_summaries] = \
                    self.run_batch(
                        sess, start_index, end_index, fetches, shuffled_padded_sequences,
                        shuffled_one_hot_labels, shuffled_text_sequence_lengths,
                        shuffled_bow_representations, None, False, current_epoch)
                all_style_embeddings.extend(style_embeddings)

            saver.save(sess=sess, save_path=global_config.model_save_path)
            writer.add_summary(all_summaries, current_epoch)
            writer.flush()

            with open(global_config.all_style_embeddings_path, 'wb') as pickle_file:
                pickle.dump(all_style_embeddings, pickle_file)

            log_msg = "Loss: [Total: {:.4f}, Rec: {:.4f}, AdvCE: {:.4f}, " \
                      "AdvE: {:.4f}, Style: {:.4f}, BOW: {:.4f}]; Epoch: {}"
            logger.info(log_msg.format(
                composite_loss, reconstruction_loss, adversarial_loss,
                adversarial_entropy, style_loss, bow_representation_loss, current_epoch))

        writer.close()

    def infer(self, sess, offset, samples_size):

        sess.run(tf.global_variables_initializer())
        saver = tf.train.Saver()
        saver.restore(sess=sess, save_path=global_config.model_save_path)

        generated_sequences = list()
        final_sequence_lengths = list()
        num_batches = samples_size // model_config.batch_size

        end_index = None
        for batch_number in range(num_batches):
            (start_index, end_index) = self.get_batch_indices(
                offset=offset, batch_number=batch_number, data_limit=(offset + samples_size))

            generated_sequences_batch, final_sequence_lengths_batch = self.run_batch(
                sess, start_index, end_index, [self.inference_output, self.final_sequence_lengths],
                self.padded_sequences, self.one_hot_labels, self.text_sequence_lengths,
                self.bow_representations, None, False, 0)

            generated_sequences.extend(generated_sequences_batch)
            final_sequence_lengths.extend(final_sequence_lengths_batch)

        return generated_sequences, final_sequence_lengths

    def generate_novel_sentences(self, sess, offset, samples_size, style_embedding):

        conditioning_embedding = np.tile(A=style_embedding, reps=(model_config.batch_size, 1))

        sess.run(tf.global_variables_initializer())
        saver = tf.train.Saver()
        saver.restore(sess=sess, save_path=global_config.model_save_path)

        generated_sequences = list()
        final_sequence_lengths = list()
        num_batches = samples_size // model_config.batch_size

        end_index = None
        for batch_number in range(num_batches):
            (start_index, end_index) = self.get_batch_indices(
                offset=offset, batch_number=batch_number, data_limit=(offset + samples_size))

            generated_sequences_batch, final_sequence_lengths_batch = self.run_batch(
                sess, start_index, end_index, [self.inference_output, self.final_sequence_lengths],
                self.padded_sequences, self.one_hot_labels, self.text_sequence_lengths,
                self.bow_representations, conditioning_embedding, True, 0)

            generated_sequences.extend(generated_sequences_batch)
            final_sequence_lengths.extend(final_sequence_lengths_batch)

        return generated_sequences, final_sequence_lengths
