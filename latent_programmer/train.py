# coding=utf-8
# Copyright 2021 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# python3
"""Train seq-to-seq model on random supervised training tasks."""

# pytype: disable=wrong-arg-count
# pytype: disable=attribute-error

import collections
import functools
import json
import os
import random
import time

from absl import app
from absl import flags
from absl import logging
from flax import jax_utils
from flax import linen as nn
from flax import optim
from flax.metrics import tensorboard
from flax.training import checkpoints
from flax.training import common_utils
import jax
import jax.numpy as jnp
import numpy as np
import tensorflow.compat.v2 as tf

from latent_programmer import decode
from latent_programmer import models
from latent_programmer.tasks.robust_fill import dsl
from latent_programmer.tasks.robust_fill import tokens as dsl_tokens
from latent_programmer.tasks.robust_fill.dataset import input_pipeline

gfile = tf.io.gfile

FLAGS = flags.FLAGS

flags.DEFINE_integer('seed', 0, 'Fixed random seed for training.')
flags.DEFINE_float('lr', 1e-3, 'Learning rate.')
flags.DEFINE_float('weight_decay', 1e-1,
                   'Decay factor for AdamW-style weight decay.')
flags.DEFINE_integer('embedding_dim', 128, 'Embedding dimension.')
flags.DEFINE_integer('hidden_dim', 512, 'Hidden dimension.')
flags.DEFINE_integer('num_heads', 4, 'Number of layers.')
flags.DEFINE_integer('num_layers', 3, 'Number of Transformer heads.')

flags.DEFINE_string('dataset_filepattern', None,
                    'Filepattern for TFRecord dataset.')
flags.DEFINE_integer('per_device_batch_size', 16,
                     'Number of program tasks in a batch.')
flags.DEFINE_integer('num_strings_per_task', 4,
                     'Number of input/output strings per task.')
flags.DEFINE_integer('max_expressions', 10,
                     'Maximum number of expressions in program.')
flags.DEFINE_integer('max_program_length', 50,
                     'Maximum number of tokens in program.')
flags.DEFINE_integer('max_characters', 100,
                     'Maximum number of characters in input/output strings.')

flags.DEFINE_string('save_dir', None, 'Directory to save results to.')
flags.DEFINE_integer('num_train_steps', 1500000, 'Number of training steps.')
flags.DEFINE_integer('num_eval_steps', 10, 'Number of evaluation steps.')
flags.DEFINE_integer('log_freq', 1000, 'Number of steps between logs.')
flags.DEFINE_integer('checkpoint_freq', 1000,
                     'Number of steps between checkpoint saves.')
flags.DEFINE_bool('restore_checkpoints', True,
                  'Whether to restore from existing model checkpoints.')


def create_learning_rate_scheduler(
    base_learning_rate=0.5,
    factors='constant * linear_warmup * rsqrt_normalized_decay',
    warmup_steps=16000,
    decay_factor=0.5,
    steps_per_decay=50000,
    steps_per_cycle=100000):
  """Creates learning rate schedule.

  Interprets factors in the factors string which can consist of:
  * constant: interpreted as the constant value,
  * linear_warmup: interpreted as linear warmup until warmup_steps,
  * rsqrt_decay: divide by square root of max(step, warmup_steps)
  * decay_every: Every k steps decay the learning rate by decay_factor.
  * cosine_decay: Cyclic cosine decay, uses steps_per_cycle parameter.

  Args:
    base_learning_rate: float, the starting constant for the lr schedule.
    factors: a string with factors separated by '*' that defines the schedule.
    warmup_steps: how many steps to warm up for in the warmup schedule.
    decay_factor: The amount to decay the learning rate by.
    steps_per_decay: How often to decay the learning rate.
    steps_per_cycle: Steps per cycle when using cosine decay.

  Returns:
    A function learning_rate(step): float -> {'learning_rate': float}, the
    step-dependent lr.
  """
  factors = [n.strip() for n in factors.split('*')]

  def step_fn(step):
    """Step to learning rate function."""
    ret = 1.0
    for name in factors:
      if name == 'constant':
        ret *= base_learning_rate
      elif name == 'linear_warmup':
        ret *= jnp.minimum(1.0, step / warmup_steps)
      elif name == 'rsqrt_decay':
        ret /= jnp.sqrt(jnp.maximum(1.0, step - warmup_steps))
      elif name == 'rsqrt_normalized_decay':
        ret *= jnp.sqrt(warmup_steps)
        ret /= jnp.sqrt(jnp.maximum(step, warmup_steps))
      elif name == 'decay_every':
        ret *= (decay_factor**(step // steps_per_decay))
      elif name == 'cosine_decay':
        progress = jnp.maximum(0.0,
                               (step - warmup_steps) / float(steps_per_cycle))
        ret *= jnp.maximum(0.0,
                           0.5 * (1.0 + jnp.cos(jnp.pi * (progress % 1.0))))
      else:
        raise ValueError('Unknown factor %s.' % name)
    return jnp.asarray(ret, dtype=jnp.float32)

  return step_fn


def compute_weighted_cross_entropy(logits, targets, weights=None):
  """Compute weighted cross entropy and entropy for log probs and targets.

  Args:
   logits: `[batch, length, num_classes]` float array.
   targets: categorical targets `[batch, length]` int array.
   weights: None or array of shape [batch, length, 1]

  Returns:
    Tuple of scalar loss and batch normalizing factor.
  """
  if logits.ndim != targets.ndim + 1:
    raise ValueError('Incorrect shapes. Got shape %s logits and %s targets' %
                     (str(logits.shape), str(targets.shape)))

  onehot_targets = common_utils.onehot(targets, logits.shape[-1])
  loss = -jnp.sum(onehot_targets * nn.log_softmax(logits), axis=-1)
  normalizing_factor = jnp.prod(jnp.asarray(targets.shape))
  if weights is not None:
    loss = loss * weights
    normalizing_factor = weights.sum()

  return loss.sum(), normalizing_factor


def compute_weighted_accuracy(logits, targets, weights=None):
  """Compute weighted accuracy for log probs and targets.

  Args:
   logits: `[batch, length, num_classes]` float array.
   targets: categorical targets `[batch, length]` int array.
   weights: None or array of shape [batch, length, 1]

  Returns:
    Tuple of scalar accuracy and batch normalizing factor.
  """
  if logits.ndim != targets.ndim + 1:
    raise ValueError('Incorrect shapes. Got shape %s logits and %s targets' %
                     (str(logits.shape), str(targets.shape)))
  acc = jnp.equal(jnp.argmax(logits, axis=-1), targets)
  normalizing_factor = jnp.prod(jnp.asarray(targets.shape))
  if weights is not None:
    acc = acc * weights
    normalizing_factor = weights.sum()

  return acc.sum(), normalizing_factor


def compute_metrics(logits, targets, weights):
  """Compute summary metrics."""
  loss, weight_sum = compute_weighted_cross_entropy(logits, targets, weights)
  acc, _ = compute_weighted_accuracy(logits, targets, weights)
  metrics = {
      'loss': loss,
      'accuracy': acc,
      'denominator': weight_sum,
  }
  metrics = jax.lax.psum(metrics, 'batch')
  return metrics


# Train / eval / decode step functions.
# -----------------------------------------------------------------------------


def train_step(optimizer,
               inputs,
               outputs,
               programs,
               learning_rate_fn,
               config,
               train_rng=None):
  """Train on batch of program tasks."""
  # We handle PRNG splitting inside the top pmap, rather
  # than handling it outside in the training loop - doing the
  # latter can add some stalls to the devices.
  train_rng, new_train_rng = jax.random.split(train_rng)

  weights = jnp.where(programs > 0, 1, 0).astype(jnp.float32)

  def loss_fn(params):
    """Loss function used for training."""
    logits = models.ProgramTransformer(config).apply(
        {'params': params},
        inputs,
        outputs,
        programs,
        rngs={'dropout': train_rng})
    loss, weight_sum = compute_weighted_cross_entropy(logits, programs, weights)
    mean_loss = loss / weight_sum
    return mean_loss, logits

  step = optimizer.state.step
  lr = learning_rate_fn(step)
  grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
  (_, logits), grad = grad_fn(optimizer.target)
  grad = jax.lax.pmean(grad, 'batch')
  new_optimizer = optimizer.apply_gradient(grad, learning_rate=lr)

  # Get metrics.
  metrics = compute_metrics(logits, programs, weights)
  metrics['learning_rate'] = lr
  return new_optimizer, metrics, new_train_rng


def eval_step(params, inputs, outputs, programs, config):
  weights = jnp.where(programs > 0, 1, 0).astype(jnp.float32)
  logits = models.ProgramTransformer(config).apply(
      {'params': params}, inputs, outputs, programs)

  return compute_metrics(logits, programs, weights)


def initialize_cache(inputs, outputs, programs, max_decode_len, config):
  """Initialize a cache for a given input shape and max decode length."""
  target_shape = (programs.shape[0], max_decode_len)
  initial_variables = models.ProgramTransformer(config).init(
      jax.random.PRNGKey(0),
      jnp.ones(inputs.shape, config.dtype),
      jnp.ones(outputs.shape, config.dtype),
      jnp.ones(target_shape, config.dtype))
  return initial_variables['cache']


def predict_step(params,
                 inputs,
                 outputs,
                 cache,
                 eos_token,
                 max_decode_len,
                 beam_size,
                 config):
  """Predict translation with fast decoding beam search on a batch."""
  # Prepare transformer fast-decoder call for beam search: for beam search, we
  # need to set up our decoder model to handle a batch size equal to
  # batch_size * beam_size, where each batch item's data is expanded in-place
  # rather than tiled.
  flat_encoded = decode.flat_batch_beam_expand(
      models.ProgramTransformer(config).apply(
          {'params': params},
          inputs,
          outputs,
          method=models.ProgramTransformer.encode),
      beam_size)

  encoded_padding_mask = jnp.where(outputs > 0, 1, 0).astype(jnp.float32)
  flat_encoded_padding_mask = decode.flat_batch_beam_expand(
      encoded_padding_mask, beam_size)

  def tokens_ids_to_logits(flat_ids, flat_cache):
    """Token slice to logits from decoder model."""
    # --> [batch * beam, 1, vocab]
    flat_logits, new_vars = models.ProgramTransformer(config).apply(
        {'params': params, 'cache': flat_cache},
        flat_ids,
        flat_encoded,
        flat_encoded_padding_mask,
        mutable=['cache'],
        method=models.ProgramTransformer.decode)
    new_flat_cache = new_vars['cache']
    # Remove singleton sequence-length dimension:
    # [batch * beam, 1, vocab] --> [batch * beam, vocab]
    flat_logits = flat_logits.squeeze(axis=1)
    return flat_logits, new_flat_cache

  # Using the above-defined single-step decoder function, run a
  # beam search over possible sequences given input encoding.
  beam_seqs, _ = decode.beam_search(
      inputs,
      cache,
      tokens_ids_to_logits,
      beam_size=beam_size,
      alpha=0.6,
      bos_token=config.bos_token,
      eos_token=eos_token,
      max_decode_len=max_decode_len)

  # Beam search returns [n_batch, n_beam, n_length] with beam dimension
  # sorted in increasing order of log-probability.
  return beam_seqs


# Util functions for prediction
# -----------------------------------------------------------------------------


def pad_examples(x, desired_batch_size):
  """Expand batch to desired size by repeating last slice."""
  batch_pad = desired_batch_size - x.shape[0]
  tile_dims = [1] * len(x.shape)
  tile_dims[0] = batch_pad
  return np.concatenate([x, np.tile(x[-1], tile_dims)], axis=0)


def tohost(x):
  """Collect batches from all devices to host and flatten batch dimensions."""
  n_device, n_batch, *remaining_dims = x.shape
  return x.reshape((n_device * n_batch,) + tuple(remaining_dims))


def per_host_sum_pmap(in_tree):
  """Execute psum on in_tree's leaves over one device per host."""
  host2devices = collections.defaultdict(list)
  for d in jax.devices():
    host2devices[d.host_id].append(d)
  devices = [host2devices[k][0] for k in host2devices]
  host_psum = jax.pmap(lambda x: jax.lax.psum(x, 'i'), 'i', devices=devices)
  def pre_pmap(xs):
    return jax.tree_map(lambda x: jnp.broadcast_to(x, (1,) + x.shape), xs)
  def post_pmap(xs):
    return jax.tree_map(lambda x: x[0], xs)
  return post_pmap(host_psum(pre_pmap(in_tree)))


def eval_predicted(predicted, inputs, outputs, parse_beam_fn):
  """Evaluate predicted program beams."""
  best_p, best_score = None, -1

  # predicted shape [beam_size, length]
  for beam in predicted:
    try:
      p = parse_beam_fn(beam)
      p_outs = [p(inp) for inp in inputs]
      score = np.sum([p_out == out for p_out, out in zip(p_outs, outputs)])
      if score > best_score:
        best_p, best_score = p, score
    except:  # pylint: disable=bare-except
      pass
    if best_score >= len(inputs):  # Found solution.
      break
  return best_p, best_score


def main(_):
  tf.enable_v2_behavior()

  tf.random.set_seed(FLAGS.seed)
  np.random.seed(FLAGS.seed)
  random.seed(FLAGS.seed)

  if not gfile.isdir(FLAGS.save_dir):
    gfile.mkdir(FLAGS.save_dir)

  hparam_str_dict = dict(seed=FLAGS.seed, lr=FLAGS.lr)
  # Get hyperparmaters
  if FLAGS.xm_parameters:
    for key, value in json.loads(FLAGS.xm_parameters).items():
      if key not in hparam_str_dict:
        hparam_str_dict[key] = value

  hparam_str = ','.join(['%s=%s' % (k, str(hparam_str_dict[k])) for k in
                         sorted(hparam_str_dict.keys())])

  # Number of local devices for this host.
  n_devices = jax.local_device_count()

  if jax.host_id() == 0:
    summary_writer = tensorboard.SummaryWriter(
        os.path.join(FLAGS.save_dir, 'tb', hparam_str))

  batch_size = FLAGS.per_device_batch_size * n_devices
  io_shape = (FLAGS.per_device_batch_size,
              FLAGS.num_strings_per_task,
              FLAGS.max_characters)
  program_shape = (FLAGS.per_device_batch_size, FLAGS.max_program_length)

  # Setup DSL
  # ---------------------------------------------------------------------------

  # Build token tables.
  id_char_table = {i+1: char for (i, char) in enumerate(dsl.CHARACTER)}
  char_id_table = {char: id for id, char in id_char_table.items()}
  id_token_table, token_id_table = dsl_tokens.build_token_tables()
  io_vocab_size = len(char_id_table) + 1  # For padding.
  program_vocab_size = len(token_id_table) + 1

  bos_token = token_id_table[dsl.BOS]
  eos_token = token_id_table[dsl.EOS]

  def decode_io(inputs, outputs):
    """Decode io examples tokens."""
    def decode_str(s):
      """Decode string tokens."""
      return ''.join([id_char_table[c_id] for c_id in s if c_id > 0])

    io_string = ''
    inps, outs = [], []
    for inp, out in zip(inputs, outputs):
      inps.append(decode_str(inp))
      outs.append(decode_str(out))
      io_string += inps[-1] + ' < ' + outs[-1] + ' > '
    return inps, outs, io_string[:-3]  # Remove last separator.

  def decode_program(program):
    """Decode program tokens."""
    program = program[:np.argmax(program == eos_token) + 1].astype(np.int32)
    try:
      p = dsl.decode_program(program, id_token_table)
      return p, p.to_string()
    except:  # pylint: disable=bare-except
      return None, ''  # Program does not compile.

  # Load Dataset
  # ---------------------------------------------------------------------------
  logging.info('Initializing dataset.')
  if not FLAGS.dataset_filepattern:
    raise ValueError('Must specify filepattern to dataset.')

  # Training dataset.
  dataset = input_pipeline.create_dataset_from_tf_record(
      FLAGS.dataset_filepattern, token_id_table, char_id_table)
  dataset = dataset.padded_batch(
      batch_size,
      padded_shapes=(io_shape[1:], io_shape[1:], program_shape[1:]),
      drop_remainder=True)
  # Split evaluation and training.
  eval_ds = dataset.take(FLAGS.num_eval_steps)
  # Decrease batch of predict dataset to handle beam search.
  predict_ds = eval_ds.unbatch().padded_batch(
      int(np.ceil(batch_size / 10)),
      padded_shapes=(io_shape[1:], io_shape[1:], program_shape[1:]))
  train_ds = dataset.skip(FLAGS.num_eval_steps).repeat()
  train_iter = train_ds.as_numpy_iterator()

  # Build Model and Optimizer
  # ---------------------------------------------------------------------------
  train_config = models.TransformerConfig(
      vocab_size=io_vocab_size,
      output_vocab_size=program_vocab_size,
      shift=True,
      emb_dim=FLAGS.embedding_dim,
      num_heads=FLAGS.num_heads,
      num_layers=FLAGS.num_layers,
      qkv_dim=FLAGS.embedding_dim,
      mlp_dim=FLAGS.hidden_dim,
      max_len=max(FLAGS.max_characters, FLAGS.max_program_length),
      deterministic=False,
      decode=False,
      bos_token=bos_token)
  eval_config = train_config.replace(deterministic=True)
  predict_config = train_config.replace(
      shift=False, deterministic=True, decode=True)

  rng = jax.random.PRNGKey(FLAGS.seed)
  rng = jax.random.fold_in(rng, jax.host_id())
  rng, init_rng = jax.random.split(rng)

  m = models.ProgramTransformer(eval_config)
  initial_variables = jax.jit(m.init)(
      init_rng,
      jnp.ones(io_shape, jnp.float32),
      jnp.ones(io_shape, jnp.float32),
      jnp.ones(program_shape, jnp.float32))

  optimizer_def = optim.Adam(
      FLAGS.lr,
      beta1=0.9,
      beta2=0.98,
      eps=1e-9,
      weight_decay=FLAGS.weight_decay)
  optimizer = optimizer_def.create(initial_variables['params'])

  del initial_variables  # Don't keep a copy of the initial model.

  start_step = 0
  if FLAGS.restore_checkpoints:
    # Restore unreplicated optimizer + model state from last checkpoint.
    optimizer = checkpoints.restore_checkpoint(
        os.path.join(FLAGS.save_dir, 'checkpoints', hparam_str), optimizer)
    # Grab last step.
    start_step = int(optimizer.state.step)
    logging.info('Found model checkpointed at step %d.', start_step)

  # Replicate optimizer.
  optimizer = jax_utils.replicate(optimizer)

  learning_rate_fn = create_learning_rate_scheduler(base_learning_rate=FLAGS.lr)
  p_train_step = jax.pmap(
      functools.partial(
          train_step,
          learning_rate_fn=learning_rate_fn,
          config=train_config),
      axis_name='batch')
  p_eval_step = jax.pmap(
      functools.partial(eval_step, config=eval_config),
      axis_name='batch')
  p_init_cache = jax.pmap(
      functools.partial(
          initialize_cache,
          max_decode_len=FLAGS.max_program_length,
          config=predict_config),
      axis_name='batch')
  p_pred_step = jax.pmap(
      functools.partial(predict_step, config=predict_config),
      axis_name='batch',
      static_broadcasted_argnums=(4, 5, 6))

  # Main Train Loop
  # ---------------------------------------------------------------------------
  train_rngs = jax.random.split(rng, jax.local_device_count())
  del rng

  metrics_all = []
  tick = time.time()
  for step in range(start_step, FLAGS.num_train_steps):
    inputs, outputs, programs = common_utils.shard(next(train_iter))

    optimizer, metrics, train_rngs = p_train_step(
        optimizer, inputs, outputs, programs, train_rng=train_rngs)
    metrics_all.append(metrics)

    # Save a Checkpoint
    if ((step % FLAGS.checkpoint_freq == 0 and step > 0) or
        step == FLAGS.num_train_steps - 1):
      if jax.host_id() == 0:
        # Save unreplicated optimizer + model state.
        checkpoints.save_checkpoint(
            os.path.join(FLAGS.save_dir, 'checkpoints', hparam_str),
            jax_utils.unreplicate(optimizer),
            step)

    # Periodic metric handling.
    if not step or step % FLAGS.log_freq != 0:
      continue

    logging.info('Gathering training metrics.')
    # Training Metrics
    metrics_all = common_utils.get_metrics(metrics_all)
    lr = metrics_all.pop('learning_rate').mean()
    metrics_sums = jax.tree_map(jnp.sum, metrics_all)
    denominator = metrics_sums.pop('denominator')
    summary = jax.tree_map(
        lambda x: x / denominator,  # pylint: disable=cell-var-from-loop
        metrics_sums)
    summary['learning_rate'] = lr
    # Calculate (clipped) perplexity after averaging log-perplexities:
    summary['perplexity'] = jnp.clip(jnp.exp(summary['loss']), a_max=1.0e4)

    if jax.host_id() == 0:
      logging.info('Train in step: %d, loss: %.4f', step, summary['loss'])
      tock = time.time()
      steps_per_sec = FLAGS.log_freq / (tock - tick)
      tick = tock
      summary_writer.scalar('train/steps per second', steps_per_sec, step)
      for key, val in summary.items():
        summary_writer.scalar('train/' + key, val, step)
      summary_writer.flush()
    # Reset metric accumulation for next evaluation cycle.
    metrics_all = []

    # Evaluation Metrics
    logging.info('Gathering evaluation metrics.')
    t_evaluation_start = time.time()
    eval_metrics = []
    for batches in eval_ds.as_numpy_iterator():
      inputs, outputs, programs = common_utils.shard(batches)

      metrics = p_eval_step(optimizer.target, inputs, outputs, programs)
      eval_metrics.append(metrics)

    eval_metrics = common_utils.get_metrics(eval_metrics)
    eval_metrics_sums = jax.tree_map(jnp.sum, eval_metrics)
    eval_denominator = eval_metrics_sums.pop('denominator')
    eval_summary = jax.tree_map(
        lambda x: x / eval_denominator,  # pylint: disable=cell-var-from-loop
        eval_metrics_sums)

    if jax.host_id() == 0:
      logging.info('Evaluation time: %.4f s step %d, loss: %.4f.',
                   time.time()-t_evaluation_start, step, eval_summary['loss'])
      for key, val in eval_summary.items():
        summary_writer.scalar('eval/' + key, val, step)
      summary_writer.flush()

    # Beam search metrics.
    logging.info('Gathering beam search metrics.')
    for beam_size in [10, 100]:
      t_inference_start = time.time()
      pred_acc = 0
      pred_denominator = 0

      ios, targets, predictions = [], [], []
      for batches in predict_ds.as_numpy_iterator():
        pred_batch = batches
        # Handle final odd-sized batch by padding instead of dropping it.
        cur_pred_batch_size = pred_batch[0].shape[0]
        if cur_pred_batch_size % n_devices:
          padded_size = int(
              np.ceil(cur_pred_batch_size / n_devices) * n_devices)
          # pylint: disable=cell-var-from-loop
          pred_batch = jax.tree_map(
              lambda x: pad_examples(x, padded_size), pred_batch)
        inputs, outputs, programs = common_utils.shard(pred_batch)

        cache = p_init_cache(inputs, outputs, programs)
        predicted = p_pred_step(optimizer.target,
                                inputs,
                                outputs,
                                cache,
                                eos_token,
                                programs.shape[-1],
                                beam_size)
        predicted = tohost(predicted)
        inputs, outputs, programs = map(tohost, (inputs, outputs, programs))

        pred_denominator += programs.shape[0]
        for i, beams in enumerate(predicted):
          inps, outs, io_string = decode_io(inputs[i], outputs[i])
          p, p_score = eval_predicted(
              beams, inps, outs,
              parse_beam_fn=lambda x: decode_program(x)[0])
          if p_score >= len(inps):
            pred_acc += 1
          ios.append(io_string)
          targets.append(decode_program(programs[i])[1])
          predictions.append(p.to_string() if p else '')

      all_pred_acc, all_pred_denominator = per_host_sum_pmap(
          jax.tree_map(np.array, (pred_acc, pred_denominator)))

      # Record beam search results as text summaries.
      message = []
      for n in np.random.choice(np.arange(len(predictions)), 8):
        text = (f'ios: {ios[n]}\n\ntarget: {targets[n]}\n\n'
                f'predicted: {predictions[n]}\n\n')
        message.append(text)

      # Write to tensorboard.
      if jax.host_id() == 0:
        logging.info('Prediction time (beam %d): %.4f s step %d, score %.4f.',
                     beam_size, time.time() - t_inference_start, step,
                     all_pred_acc / all_pred_denominator)
        summary_writer.scalar('predict/score-{}'.format(beam_size),
                              all_pred_acc / all_pred_denominator, step)
        summary_writer.text('samples-{}'.format(beam_size),
                            '\n------\n'.join(message), step)
        summary_writer.flush()


if __name__ == '__main__':
  app.run(main)
