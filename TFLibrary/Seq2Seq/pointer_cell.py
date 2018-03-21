import tensorflow as tf
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import rnn as rnn_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import tensor_array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.contrib.framework.python.framework import tensor_util
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.layers import base as layers_base
from tensorflow.python.layers import core as layers_core
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import check_ops
from tensorflow.python.ops import clip_ops
from tensorflow.python.ops import functional_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import rnn_cell_impl
from tensorflow.python.ops import tensor_array_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.util import nest

from tensorflow.python.layers import core as core_layers
from tensorflow.python.ops.distributions import categorical
from tensorflow.contrib.seq2seq.python.ops import attention_wrapper as seq2seq_ops

from collections import namedtuple
from TFLibrary.Seq2Seq import attention_utils
from TFLibrary.Seq2Seq import rnn_cell_utils

ZERO_TOLERANCE = 1e-6


def _is_zero_matrix(X):
    # taking into account of small numerical errors
    mat_sum = tf.reduce_sum(X)
    return tf.less(mat_sum, ZERO_TOLERANCE)


def _calc_final_dist(vocab_dist, attn_dist, p_gen,
                     batch_size, vocab_size, num_source_OOVs,
                     enc_batch_extended_vocab):
    # P(gen) x P(vocab)
    weighted_P_vocab = p_gen * vocab_dist
    # (1 - P(gen)) x P(attention)
    weighted_P_copy = (1 - p_gen) * attn_dist
    
    # get the word-idx for all words
    extended_vsize = vocab_size + num_source_OOVs
    # placeholders to OOV words
    extra_zeros = tf.zeros((batch_size, num_source_OOVs))
    # this distribution span the entire words
    weighted_P_vocab_extended = tf.concat(
        axis=1, values=[weighted_P_vocab, extra_zeros])
    
    # assign probabilities from copy distribution
    # into correspodning positions in extended_vocab_dist
    
    # to do this, we need to use scatter_nd
    # scatter_nd (in this case) requires two numbers
    # one is the index in batch-dimension
    # the other is the index in vocab-dimension
    # So first, we create a batch-matrix like:
    # [[1, 1, 1, 1, 1, ...],
    #  [2, 2, 2, 2, 2, ...],
    #  [...]
    #  [N, N, N, N, N, ...]]
    
    # [1, 2, ..., N]
    # to [[1], [2], ..., [N]]
    # and finally to the final shape
    enc_seq_len = tf.shape(enc_batch_extended_vocab)[1]
    batch_nums = tf.range(0, limit=batch_size)
    batch_nums = tf.expand_dims(batch_nums, 1)
    batch_nums = tf.tile(batch_nums, [1, enc_seq_len])
    
    # stick together batch-dim and index-dim
    indices = tf.stack((batch_nums, enc_batch_extended_vocab), axis=2)
    scatter_shape = [batch_size, extended_vsize]
    # scatter the attention distributions
    # into the word-indices
    weighted_P_copy_projected = tf.scatter_nd(
        indices, weighted_P_copy, scatter_shape)
    
    # Add the vocab distributions and the copy distributions together
    # to get the final distributions, final_dists is a list length
    # max_dec_steps; each entry is (batch_size, extended_vsize)
    # giving the final distribution for that decoder timestep
    # Note that for decoder timesteps and examples corresponding to
    # a [PAD] token, this is junk - ignore.
    final_dists = weighted_P_vocab_extended + weighted_P_copy_projected

    return final_dists



_zero_state_tensors = rnn_cell_impl._zero_state_tensors
PointerWrapperState = namedtuple("PointerWrapperState",
    ("cell_state", "time",  # RNN Cells
     "attention", "alignments", "alignment_history",  # Attentions
     "p_gen", "coverage", "p_gen_history", "coverage_history"))  # Pointers



class PointerWrapperState(rnn_cell_impl.RNNCell):
    def __init__(self,
                 cell,
                 # attention
                 attention_mechanism,
                 attention_layer_size=None,
                 alignment_history=False,
                 cell_input_fn=None,
                 output_attention=True,
                 # pointer
                 coverage=False,
                 # misc
                 initial_cell_state=None,
                 name=None,
                 attention_scope=None,
                 pointer_scope=None):

        super(PointerWrapperState, self).__init__(name=name)
        if not rnn_cell_impl._like_rnncell(cell):  # pylint: disable=protected-access
            raise TypeError(
                "cell must be an RNNCell, saw type: %s" % type(cell).__name__)
        
        if not isinstance(attention_mechanism, seq2seq_ops.AttentionMechanism):
            raise TypeError(
                "attention_mechanism must be an AttentionMechanism or list of "
                "multiple AttentionMechanism instances, saw type: %s"
                % type(attention_mechanism).__name__)

        if cell_input_fn is None:
            cell_input_fn = (lambda inputs, attention:
                array_ops.concat([inputs, attention], -1))
        else:
            if not callable(cell_input_fn):
                raise TypeError(
                    "cell_input_fn must be callable, saw type: %s"
                    % type(cell_input_fn).__name__)

        if attention_layer_size is not None:
            # assert this for now
            # but can be relaxed in future versions
            if attention_layer_size != cell.output_size:
                raise ValueError("Current version of PointerWrapper"
                    " only supports having same attention layer size"
                    " with cell.output_size")

            attention_layer = layers_core.Dense(
                units=attention_layer_size,
                name="attention_layer",
                use_bias=False,
                dtype=attention_mechanism.dtype)

        if coverage:
            raise NotImplementedError("Not Supported")

        if initial_cell_state:
            raise NotImplementedError("Not Supported")

        pgen_layer = core_layers.Dense(
            units=1, activation=tf.sigmoid,
            use_bias=True, name="pgen_kernel")

        self._cell = cell
        self._attention_mechanism = attention_mechanism
        self._attention_layer_size = attention_layer_size
        self._cell_input_fn = cell_input_fn
        self._output_attention = output_attention
        self._alignment_history = alignment_history
        self._attention_scope = attention_scope
        self._pointer_scope = pointer_scope

        self._attention_layer = attention_layer
        self._pgen_layer = pgen_layer
    
    @property
    def output_size(self):
        if self._output_attention:
            return self._attention_layer_size
        else:
            return self._cell.output_size

    @property
    def state_size(self):
        """The `state_size` property of `PointerWrapperState`.
        Returns:
            An `PointerWrapperState` of shapes used by this object.
        """
        return PointerWrapperState(
            # Cells
            cell_state=self._cell.state_size,
            time=tensor_shape.TensorShape([]),
            # Attentions
            attention=self._attention_layer_size,
            alignments=self._attention_mechanism.alignments_size,
            alignment_history=(),  # sometimes a TensorArray
            # Pointers
            # p_gen \in [0,1]
            p_gen=tensor_shape.TensorShape([1]),
            # coverage = sum(alignments)
            coverage=self._attention_mechanism.alignments_size,
            p_gen_history=(),
            coverage_history=())



    def zero_state(self, batch_size, dtype):
        """Return an initial (zero) state tuple for this `AttentionWrapper`.
        **NOTE** Please see the initializer documentation for details of how
        to call `zero_state` if using an `AttentionWrapper` with a
        `BeamSearchDecoder`.
        Args:
          batch_size: `0D` integer tensor: the batch size.
          dtype: The internal state data type.
        Returns:
          An `AttentionWrapperState` tuple containing zeroed out tensors and,
          possibly, empty `TensorArray` objects.
        Raises:
          ValueError: (or, possibly at runtime, InvalidArgument), if
            `batch_size` does not match the output size of the encoder passed
            to the wrapper object at initialization time.
        """
        zs_name = type(self).__name__ + "ZeroState"
        with ops.name_scope(zs_name, values=[batch_size]):
            zero_attention = _zero_state_tensors(
                state_size=self._attention_layer_size,
                batch_size=batch_size,
                dtype=dtype)
            zero_alignments = self._attention_mechanism.initial_alignments(
                batch_size=batch_size, dtype=dtype)
            zero_alignment_history = (tensor_array_ops.TensorArray(
                dtype=dtype, size=0, dynamic_size=True)
                if self._alignment_history else ())
            zero_coverage = self._attention_mechanism.initial_alignments(
                batch_size=batch_size, dtype=dtype)
            zero_p_gen_history = (tensor_array_ops.TensorArray(
                dtype=dtype, size=0, dynamic_size=True)
                if self._alignment_history else ())
            zero_coverage_history = (tensor_array_ops.TensorArray(
                dtype=dtype, size=0, dynamic_size=True)
                if self._alignment_history else ())

            return PointerWrapperState(
                # Cells
                cell_state=self._cell.zero_state(batch_size, dtype),
                time=array_ops.zeros([], dtype=dtypes.int32),
                # Attention
                attention=zero_attention,
                alignments=zero_alignments,
                alignment_history=zero_alignment_history,
                # Pointers
                p_gen=array_ops.zeros([1], dtype=dtypes.int32),
                coverage=zero_coverage,
                p_gen_history=zero_p_gen_history,
                coverages_history=zero_coverage_history)
    
    def call(self, inputs, state):
        if not isinstance(state, PointerWrapperState):
            raise TypeError("Expected state to be instance "
                "of AttentionWrapperState. "
                "Received type %s instead." % type(state))

        # Step 1: Calculate the true inputs to the cell based on the
        # previous attention value.
        cell_inputs = self._cell_input_fn(inputs, state.attention)
        cell_state = state.cell_state
        cell_output, next_cell_state = self._cell(cell_inputs, cell_state)
        last_layer_state = rnn_cell_utils.get_last_layer_cell_state(
            cell_states=next_cell_state)

        # Step 2: Compute attention and write to history
        with variable_scope.variable_scope(
                self._attention_scope, reuse=tf.AUTO_REUSE):

            attention, alignments, context = attention_utils._compute_attention(
                attention_mechanism=self._attention_mechanism,
                cell_output=cell_output,
                attention_state=None,
                attention_layer=(self._attention_layer
                                 if self._attention_layer else None))

            alignment_history = state.alignment_history.write(
                state.time, alignments) if self._alignment_history else ()

        # Step 3: Compute pointer and coverage and write to histories
        with variable_scope.variable_scope(
                self._pointer_scope, reuse=tf.AUTO_REUSE):

            p_gen = self._pgen_layer(array_ops.concat([
                context, last_layer_state.c,
                last_layer_state.h, cell_inputs], -1))

            # update p_gens_history distributions
            p_gen_history = state.p_gen_history.write(
                state.time, p_gen)


        next_state = PointerWrapperState(
            # Cells
            time=state.time + 1,
            cell_state=next_cell_state,
            # Attention
            attention=attention,
            alignments=alignments,
            alignment_history=alignment_history,
            # Pointer
            p_gen=p_gen,
            coverage=state.coverage,
            p_gen_history=p_gen_history,
            coverages_history=state.coverages_history)
    
        if self._output_attention:
            return attention, next_state
        else:
            return cell_output, next_state
















def policy_gradient_pointer_attention_decoder(
        cell,
        scope,
        memory,
        decoder_inputs,
        initial_state,
        enc_padding_mask,
        prev_coverage=None,
        # tokens
        UNK_token=0,
        start_tokens=None,
        embeddings=None,
        vocab_size=50000,
        num_source_OOVs=None,
        enc_batch_extended_vocab=None,
        # some flags
        reinforce=False,
        pointer_gen=True,
        use_coverage=False,
        debug_mode=False,
        # for decoding
        initial_state_attention=False):
    """PolicyGradient decoder"""

    # some todo's
    # if initial_state_attention:
    #     raise NotImplementedError
    if use_coverage or prev_coverage:
        # there is a problem in coverage, see Line 216 - 221 in original attention_decoder
        raise NotImplementedError

    if reinforce and ((embeddings is None) or (start_tokens is None)):
        raise ValueError("when using reinforce, "
            "please provide embeddings and start_tokens")

    print("TODO: Using tf.where to replace tf.cond in next_cell_input")
    print("change sampled_tokens not include <start>?")

    # input data
    max_time = decoder_inputs.get_shape()[1].value
    attn_size = memory.get_shape()[2].value
    batch_size = memory.get_shape()[0].value
    input_size = decoder_inputs.get_shape()[2].value
    sequence_length = array_ops.tile([max_time], [batch_size])
    inputs_ta = tf.TensorArray(dtype=tf.float32, size=max_time)
    # TensorArray will unstack first dimension
    inputs_ta = inputs_ta.unstack(tf.transpose(decoder_inputs, perm=[1, 0, 2]))

    with variable_scope.variable_scope(scope.Attention):
        # layers
        # To calculate attention, we calculate
        #   v^T tanh(W_h h_i + W_s s_t + b_attn)
        # where h_i is an encoder state, and s_t a decoder state.
        # attn_vec_size is the length of the vectors v, b_attn, (W_h h_i) and (W_s s_t).
        # We set it to be equal to the size of the encoder states.
        attention_vec_size = attn_size
        # memory kernel maps encoder hidden states into memory
        memory_kernel = core_layers.Dense(
            units=attention_vec_size,
            use_bias=False, name="memory_kernel")
        # query kernel maps decoder hidden state into query
        query_kernel = core_layers.Dense(
            units=attention_vec_size,
            use_bias=True, name="query_kernel")
        # input kernel maps decoder hidden state into query
        input_kernel = core_layers.Dense(
            units=input_size,
            use_bias=True, name="input_kernel")
        # pgen_kernel maps states into p_gen
        pgen_kernel = core_layers.Dense(
            units=1, activation=tf.sigmoid,
            use_bias=True, name="pgen_kernel")
        # output_kernel maps cell_outputs into final cell outputs
        output_kernel = core_layers.Dense(
            units=cell.output_size,
            use_bias=True, name="output_kernel")
        # coverage kernels transforms coverage vector
        coverage_kernel = core_layers.Dense(
            units=attention_vec_size,
            use_bias=False, name="coverage_kernel")
        # output_kernel maps cell_outputs into final cell outputs
        logits_kernel = core_layers.Dense(
            units=vocab_size,
            use_bias=True, name="logits_kernel")
        
        # Get the weight matrix W_h and apply it to each encoder state to get
        # (W_h h_i), the encoder features
        # shape (batch_size,attn_length,1,attention_vec_size)
        processed_memory = memory_kernel(memory)

        def masked_attention(score):
            """Softmax + enc_padding_mask + re-normalize"""
            # take softmax. shape (batch_size, attn_length)
            attn_dist = nn_ops.softmax(score)
            attn_dist *= enc_padding_mask
            # shape (batch_size)
            masked_sums = math_ops.reduce_sum(attn_dist, axis=1)
            # re-normalize
            return attn_dist / array_ops.reshape(masked_sums, [-1, 1])

        def _compute_attention(cell_output, coverage=None):
            # Pass the decoder state through a linear layer
            # (this is W_s s_t + b_attn in the paper)
            # shape (batch_size, attention_vec_size)
            processed_query = control_flow_ops.cond(
                # i.e. None or not set
                _is_zero_matrix(coverage),
                # v^T tanh(W_h h_i + W_s s_t + b_attn)
                true_fn=lambda: query_kernel(cell_output),
                # v^T tanh(W_h h_i + W_s s_t + w_c c_i^t + b_attn)
                false_fn=lambda: (query_kernel(cell_output) +
                                  coverage_kernel(coverage)))

            score = attention_utils._bahdanau_score(
                processed_query=processed_query,
                keys=processed_memory,
                normalize=False)

            # Calculate attention distribution
            alignments = masked_attention(score)

            if use_coverage:
                # update coverage
                coverage = coverage + alignments

            # Reshape from [batch_size, memory_time]
            # to [batch_size, 1, memory_time]
            expanded_alignments = array_ops.expand_dims(alignments, 1)
            # Context is the inner product of alignments and values along the
            # memory time dimension.
            # alignments shape is
            #   [batch_size, 1, memory_time]
            # attention_mechanism.values shape is
            #   [batch_size, memory_time, memory_size]
            # the batched matmul is over memory_time, so the output shape is
            #   [batch_size, 1, memory_size].
            # we then squeeze out the singleton dim.
            context = math_ops.matmul(expanded_alignments, memory)
            context = array_ops.squeeze(context, [1])

            return context, alignments, coverage

        def loop_fn(loop_time, cell_output, cell_state, loop_state):
            if cell_output is None:  # time == 0
                final_dist = None
                emit_output = final_dist  # == None for time == 0
                next_cell_state = initial_state  # encoder last states
                coverage = (array_ops.zeros([batch_size, attn_size])
                            if prev_coverage is None else prev_coverage)

                # convext vector will initially be zeros
                # Ensure the second shape of attention vectors is set.
                context_vector = array_ops.zeros([batch_size, attn_size])
                context_vector.set_shape([None, attn_size])

                if initial_state_attention:
                    with variable_scope.variable_scope(
                            scope.Attention, reuse=tf.AUTO_REUSE):
                        # true in decode mode
                        # Re-calculate the context vector from the previous
                        # step so that we can pass it through a linear layer
                        # with this step's input to get a modified version of
                        # the input in decode mode, this is what updates the
                        # coverage vector
                        context_vector, _, coverage = _compute_attention(
                            cell_output=next_cell_state[-1].h,
                            coverage=coverage)

                # all TensorArrays for recoding sequences
                outputs_history = tensor_array_ops.TensorArray(
                    dtype=tf.float32, size=0, dynamic_size=True)
                alignments_history = tensor_array_ops.TensorArray(
                    dtype=tf.float32, size=0, dynamic_size=True)
                p_gens_history = tensor_array_ops.TensorArray(
                    dtype=tf.float32, size=0, dynamic_size=True)
                coverages_history = tensor_array_ops.TensorArray(
                    dtype=tf.float32, size=0, dynamic_size=True)
                sampled_tokens_history = tensor_array_ops.TensorArray(
                    dtype=tf.int32, size=0, dynamic_size=True)
                
                # mostly used in debugging
                logits_history = tensor_array_ops.TensorArray(
                    dtype=tf.float32, size=0, dynamic_size=True)
                vocab_dists_history = tensor_array_ops.TensorArray(
                    dtype=tf.float32, size=0, dynamic_size=True)
                final_dists_history = tensor_array_ops.TensorArray(
                    dtype=tf.float32, size=0, dynamic_size=True)
                
            else:
                # normal workflow:
                # decoder_inputs = input_kernel(inputs; context)
                # cell_output, states = cell(decoder_inputs, states)
                # context, att_dist, coverage = attention(states, coverage)
                # p_gen = pgen_kernel(...)
                # cell_outputs = output_kernel(cell_output, context)

                # since raw-rnn encapsulates cell call
                # we do this:
                # context, att_dist, coverage = attention(states, coverage)
                # p_gen = pgen_kernel(...)
                # cell_outputs = output_kernel(cell_output, context)
                # next_inputs = input_kernel(inputs; context) --> changed
                # Run the attention mechanism.

                # no change
                next_cell_state = cell_state

                # get the cell state of last layer's cell
                last_layer_state = cell_state[-1]

                # cell_input is cell inputs
                (sampled_tokens_history,
                 outputs_history, alignments_history, p_gens_history,
                 coverages_history, logits_history, vocab_dists_history,
                 final_dists_history, coverage, cell_input) = loop_state

                # Run the attention mechanism.
                with variable_scope.variable_scope(
                        scope.Attention, reuse=tf.AUTO_REUSE):
                    # reuse=initial_state_attention or i > 0
                    # or scope.Attention.reuse):
                    context_vector, attn_dist, coverage = _compute_attention(
                        cell_output=cell_output, coverage=coverage)
                    
                    # Concatenate the cell_output (= decoder state)
                    # and the context vector, and pass them through
                    # a linear layer. This is V[s_t, h*_t] + b in the paper
                    attention_output = output_kernel(
                        array_ops.concat([cell_output, context_vector], -1))

                    # update attention and cell_outputs
                    outputs_history = outputs_history.write(
                        loop_time - 1, attention_output)
                    alignments_history = alignments_history.write(
                        loop_time - 1, attn_dist)
                    coverages_history = coverages_history.write(
                        loop_time - 1, coverage)

                # Calculate p_gen
                if pointer_gen:
                    with variable_scope.variable_scope(scope.Pointer):
                        p_gen = pgen_kernel(array_ops.concat([
                            context_vector, last_layer_state.c,
                            last_layer_state.h, cell_input], -1))
                        # update p_gens_history distributions
                        p_gens_history = p_gens_history.write(
                            loop_time - 1, p_gen)

                # reuse variables
                # probably not necessary
                # [scope.Decoder[i].reuse_variables()
                #     for i in range(len(scope.Decoder))]
                # scope.Attention.reuse_variables()
                # scope.Pointer.reuse_variables()

                # distribution
                logits = logits_kernel(attention_output)
                vocab_dist = nn_ops.softmax(logits)
                
                final_dist = _calc_final_dist(
                    vocab_dist=vocab_dist,
                    attn_dist=attn_dist,
                    p_gen=p_gen,
                    batch_size=batch_size,
                    vocab_size=vocab_size,
                    num_source_OOVs=num_source_OOVs,
                    enc_batch_extended_vocab=enc_batch_extended_vocab)

                # raw_rnn requires `emit_output` to have same
                # shape with cell.output_size
                # thus we have to output attention_output
                # but not the final_distribution
                emit_output = attention_output

                # save these for debugging
                logits_history = logits_history.write(
                    loop_time - 1, logits)
                vocab_dists_history = vocab_dists_history.write(
                    loop_time - 1, vocab_dist)
                final_dists_history = final_dists_history.write(
                    loop_time - 1, final_dist)

            # generic
            elements_finished = (loop_time >= sequence_length)
            finished = math_ops.reduce_all(elements_finished)

            if reinforce and not initial_state_attention:
                # see Google's code
                # elements_finished = tf.logical_or(
                #     tf.equal(chosen_outputs, misc.BF_EOS_INT),
                #     loop_time >= global_config.timestep_limit)
                # they have this logical_or to stop
                # generation when sampled STOP
                # I am ignoring this for now, but probably
                # look back on this later?

                # also, Google used prev_elements_finished
                # but I used elements_finished, is that correct?

                if cell_output is None:  # time == 0
                    # when time == 0, use start_tokens
                    tf.logging.info("Running RLModel")
                    chosen_outputs = start_tokens
                else:
                    def _multinomial_sample(probs):
                        # tf.multinomial only samples from
                        # logits (unnormalized probability)
                        # here we only have normalized probability
                        # thus we use distributions.Categorical
                        dist = categorical.Categorical(probs=probs)

                        # use argmax during debugging
                        if not debug_mode:
                            sampled_tokens = dist.sample()
                        else:
                            sampled_tokens = dist.mode()

                        # since final_dist = vocab_dist + copy_dist
                        # sampled_tokens can have index out-of vocab_dist
                        # in this case we cast them into UNK
                        UNKs = array_ops.ones_like(sampled_tokens) * UNK_token
                        sampled_tokens = array_ops.where(
                            math_ops.greater(sampled_tokens, vocab_size),
                            UNKs, sampled_tokens, name="sampled_tokens")

                        return sampled_tokens

                    # otherwise, do the sampling in sequence_length
                    chosen_outputs = tf.to_int32(array_ops.where(
                        elements_finished,
                        array_ops.zeros([batch_size], dtype=tf.int32),
                        _multinomial_sample(final_dist)))

                    sampled_tokens_history = sampled_tokens_history.write(
                        loop_time - 1, chosen_outputs)

                next_input = array_ops.gather(embeddings, chosen_outputs)
            else:
                next_input = control_flow_ops.cond(
                    finished,
                    lambda: array_ops.zeros(
                        [batch_size, input_size], dtype=tf.float32),
                    lambda: inputs_ta.read(loop_time))

            with variable_scope.variable_scope(scope.Attention):
                # next inputs = input_kernel(inp; context)
                next_cell_input = input_kernel(
                    array_ops.concat([next_input, context_vector], -1))

            next_loop_state = (
                sampled_tokens_history,
                outputs_history, alignments_history, p_gens_history,
                coverages_history, logits_history, vocab_dists_history,
                final_dists_history, coverage, next_cell_input)
            
            return (elements_finished, next_cell_input, next_cell_state,
                    emit_output, next_loop_state)


        with tf.variable_scope("policy"):
            (decoder_outputs_ta,
             final_cell_state,
             final_loop_state) = rnn_ops.raw_rnn(
                cell=cell, loop_fn=loop_fn)

            (sampled_tokens_history,
             outputs_history, alignments_history, p_gens_history,
             coverages_history, logits_history, vocab_dists_history,
             final_dists_history, coverage, cell_input) = final_loop_state

        # [time, batch, nun_units] to [batch, time, num_units]
        final_dists = array_ops.transpose(
            final_dists_history.stack(), perm=[1, 0, 2])
        attn_dists = array_ops.transpose(
            alignments_history.stack(), perm=[1, 0, 2])
        p_gens = array_ops.transpose(
            p_gens_history.stack(), perm=[1, 0, 2])

        sampled_tokens = None
        if reinforce:
            sampled_tokens = array_ops.transpose(
                sampled_tokens_history.stack(), perm=(1, 0))

        # HG: what is that?
        # If using coverage, reshape it
        if coverage is not None:
            coverage = array_ops.reshape(coverage, [batch_size, -1])

        # used in debugging
        debug_variables = {
            "memory_kernel": memory_kernel,
            "query_kernel": query_kernel,
            "input_kernel": input_kernel,
            "pgen_kernel": pgen_kernel,
            "output_kernel": output_kernel,
            "coverage_kernel": coverage_kernel,
            "logits_kernel": logits_kernel,
            "memory": memory,
            "processed_memory": processed_memory}

        return (final_dists, final_cell_state, attn_dists, p_gens,
                coverage, sampled_tokens, decoder_outputs_ta,
                debug_variables, final_loop_state)
