"""
This is a module to be used as a reference for building other modules
"""
import numpy as np
import numba
from sklearn.base import BaseEstimator, TransformerMixin
import itertools
import pandas as pd
from sklearn.utils.validation import check_X_y, check_array, check_is_fitted
from collections import defaultdict
import scipy.sparse

from .utils import flatten


@numba.njit(nogil=True)
def construct_token_dictionary_and_frequency(token_sequence, token_dictionary=None):
    n_tokens = len(token_sequence)
    if token_dictionary is None:
        unique_tokens = sorted(list(set(token_sequence)))
        token_dictionary = dict(zip(unique_tokens, range(len(unique_tokens))))

    index_list = [
        token_dictionary[token] for token in token_sequence if token in token_dictionary
    ]
    token_counts = np.bincount(index_list).astype(np.float32)

    token_frequency = token_counts / n_tokens

    return token_dictionary, token_frequency, n_tokens


@numba.njit(nogil=True)
def prune_token_dictionary(
    token_dictionary,
    token_frequencies,
    ignored_tokens=None,
    min_frequency=0.0,
    max_frequency=1.0,
):

    if ignored_tokens is not None:
        tokens_to_prune = set(ignored_tokens)
    else:
        tokens_to_prune = set([])

    reverse_vocabulary = {index: word for word, index in token_dictionary.items()}

    infrequent_tokens = np.where(token_frequencies <= min_frequency)[0]
    frequent_tokens = np.where(token_frequencies >= max_frequency)[0]

    tokens_to_prune.update({reverse_vocabulary[i] for i in infrequent_tokens})
    tokens_to_prune.update({reverse_vocabulary[i] for i in frequent_tokens})

    vocab_tokens = [token for token in token_dictionary if token not in tokens_to_prune]
    new_vocabulary = dict(zip(vocab_tokens, range(len(vocab_tokens))))
    new_token_frequency = np.array(
        [token_frequencies[token_dictionary[token]] for token in new_vocabulary]
    )

    return new_vocabulary, new_token_frequency


@numba.njit(nogil=True)
def preprocess_token_sequences(
    token_sequences,
    token_dictionary=None,
    min_occurrences=None,
    max_occurrences=None,
    min_frequency=None,
    max_frequency=None,
    ignored_tokens=None,
):
    flat_sequence = flatten(token_sequences)

    # Get vocabulary and word frequencies
    (
        token_dictonary,
        token_frequencies,
        total_tokens,
    ) = construct_token_dictionary_and_frequency(flat_sequence, token_dictionary)

    if min_occurrences is None:
        if min_frequency is None:
            min_frequency = 0.0
    else:
        if min_frequency is not None:
            assert min_occurrences / total_tokens == min_frequency
        else:
            min_frequency = min_occurrences / total_tokens

    if max_occurrences is None:
        if max_frequency is None:
            max_frequency = 1.0
    else:
        if max_frequency is not None:
            assert max_occurrences / total_tokens == max_frequency
        else:
            max_frequency = min(1.0, max_occurrences / total_tokens)

    token_dictionary, token_frequencies = prune_token_dictionary(
        token_dictionary,
        token_frequencies,
        ignored_tokens=ignored_tokens,
        min_frequency=min_frequency,
        max_frequency=max_frequency,
    )

    result_sequences = [
        np.array(
            [token_dictionary[token] for token in sequence if token in token_dictionary]
        )
        for sequence in token_sequences
    ]

    return result_sequences, token_dictionary, token_frequencies


@numba.njit(nogil=True)
def information_window(token_sequence, desired_entropy, token_frequency):
    result = []

    for i in range(len(token_sequence)):
        counter = 0
        current_entropy = 0.0

        for j in range(i + 1, len(token_sequence)):
            current_entropy -= np.log(token_frequency[int(token_sequence[j])])
            counter += 1
            if current_entropy >= desired_entropy:
                break

        result.append(token_sequence[i + 1 : i + 1 + counter])

    return result


@numba.njit(nogil=True)
def fixed_window(token_sequence, window_size):
    result = []

    for i in range(len(token_sequence)):
        result.append(token_sequence[i + 1 : i + window_size + 1])

    return result


@numba.njit(nogil=True)
def flat_kernel(window):
    return np.ones(len(window), dtype=np.float32)


@numba.njit(nogil=True)
def triangle_kernel(window, window_size):
    start = max(window_size, len(window))
    stop = window_size - len(window)
    return np.arange(start, stop, -1).astype(np.float32)


@numba.njit(nogil=True)
def harmonic_kernel(window):
    result = np.arange(1, len(window) + 1).astype(np.float32)
    return 1.0 / result


@numba.njit(nogil=True)
def build_skip_grams(
    token_sequence, window_function, kernel_function, window_args, kernel_args
):
    original_tokens = token_sequence
    n_original_tokens = len(original_tokens)

    if n_original_tokens < 2:
        return np.zeros((1, 3), dtype=np.float32)

    windows = window_function(token_sequence, *window_args)

    new_tokens = np.empty(
        (np.sum(np.array([len(w) for w in windows])), 3), dtype=np.float32
    )
    new_token_count = 0

    for i in range(n_original_tokens):
        head_token = original_tokens[i]
        window = windows[i]
        weights = kernel_function(window, *kernel_args)

        for j in range(len(window)):
            new_tokens[new_token_count, 0] = numba.types.float32(head_token)
            new_tokens[new_token_count, 1] = numba.types.float32(window[j])
            new_tokens[new_token_count, 2] = weights[j]
            new_token_count += 1

    return new_tokens


@numba.njit(nogil=True, parallel=True)
def sequence_skip_grams(
    token_sequences, window_function, kernel_function, window_args, kernel_args
):
    skip_grams_per_sequence = [
        build_skip_grams(
            token_sequence, window_function, kernel_function, window_args, kernel_args
        )
        for token_sequence in token_sequences
    ]
    return np.vstack(skip_grams_per_sequence)


def token_cooccurence_matrix(
    token_sequences,
    window_function=fixed_window,
    kernel_function=flat_kernel,
    window_args=(5,),
    kernel_args=(),
    token_dictionary=None,
    symmetrize=False,
):

    raw_coo_data = sequence_skip_grams(
        token_sequences, window_function, kernel_function, window_args, kernel_args
    )
    cooccurrence_matrix = scipy.sparse.coo_matrix(
        (
            raw_coo_data.T[2],
            (raw_coo_data.T[0].astype(np.int64), raw_coo_data.T[1].astype(np.int64)),
        ),
        dtype=np.float32,
    )
    if symmetrize:
        cooccurrence_matrix = cooccurrence_matrix + cooccurrence_matrix.transpose()

    index_dictionary = {index: token for token, index in token_dictionary.items()}

    return cooccurrence_matrix.tocsr(), token_dictionary, index_dictionary


class TokenCooccurrenceVectorizer(BaseEstimator, TransformerMixin):
    """Given a sequence, or list of sequences of tokens, produce a
    co-occurrence count matrix of tokens. If passed a single sequence of tokens it
    will use windows to determine co-occurrence. If passed a list of sequences of
    tokens it will use windows within each sequence in the list -- with windows not
    extending beyond the boundaries imposed by the individual sequences in the list."""

    def __init__(
        self,
        token_dictionary=None,
        min_occurrences=None,
        max_occurrences=None,
        min_frequency=None,
        max_frequency=None,
        ignored_tokens=None,
        window_function=fixed_window,
        kernel_function=flat_kernel,
        window_args=(5,),
        kernel_args=(),
        symmetrize=False,
    ):
        self.token_dictionary = token_dictionary
        self.min_occurrences = min_occurrences
        self.min_frequency = min_frequency
        self.max_occurrences = max_occurrences
        self.max_frequency = max_frequency
        self.ignored_tokens = ignored_tokens

        self.window_function = window_function
        self.kernel_function = kernel_function
        self.window_args = window_args
        self.kernel_args = kernel_args

        self.symmetrize = symmetrize

    def fit_transform(self, X, y=None, **fit_params):

        (
            token_sequences,
            self.token_dictionary_,
            self.token_frequencies_,
        ) = preprocess_token_sequences(
            X,
            self.token_dictionary,
            min_occurrences=self.min_occurrences,
            max_occurrences=self.max_occurrences,
            min_frequency=self.min_frequency,
            max_frequency=self.max_frequency,
            ignored_tokens=self.ignored_tokens,
        )
        if self.window_function is information_window:
            window_args = (*self.window_args, self.token_frequencies_)
        else:
            window_args = self.window_args

        (
            self.cooccurrences_,
            self.token_dictionary_,
            self.index_dictionary_,
        ) = token_cooccurence_matrix(
            X,
            window_function=self.window_function,
            kernel_function=self.kernel_function,
            window_args=window_args,
            kernel_args=self.kernel_args,
            symmetrize=self.symmetrize,
        )

        return self.cooccurrences_

    def fit(self, X, y=None, **fit_params):
        self.fit_transform(X, y)
        return self


class DistributionVectorizer(BaseEstimator, TransformerMixin):
    pass

def find_bin_boundaries(flat, n_bins):
    """
    Only uniform distribution is currently implemented.
    TODO: Implement Normal
    :param flat: an iterable.
    :param n_bins:
    :return:
    """
    flat.sort()
    flat_csum = np.cumsum(flat)
    bin_range = flat_csum[-1]/n_bins
    bin_indices = [0]
    for i in range(1, len(flat_csum)):
        if( (flat_csum[i]>=bin_range*len(bin_indices)) & (flat[i] > flat[bin_indices[-1]]) ):
            bin_indices.append(i)
    bin_values= np.array(flat,dtype=float)[bin_indices]
    return bin_values

def expand_boundaries(my_interval_index, absolute_range):
    """
    expands the outer bind on a pandas IntervalIndex to encompase the range specified by the 2-tuple absolute_range
    :param my_interval_index:
    :param absolute_range: 2tuple (min_value, max_value)
    :return: a pandas IntervalIndex
    """
    interval_list = my_interval_index.to_list()
    #Check if the left boundary needs expanding
    if interval_list[0].left>absolute_range[0]:
        interval_list[0] = pd.Interval(left=absolute_range[0],
                                       right=interval_list[0].right)
    #Check if the right boundary needs expanding
    last = len(interval_list)-1
    if interval_list[last].right<absolute_range[1]:
        interval_list[last] = pd.Interval(left=interval_list[last].left,
                                       right=absolute_range[1])
    return pd.IntervalIndex(interval_list)


def add_outier_bins(my_interval_index, absolute_range):
    """
    Appends extra bins to either side our our interval index if appropriate.
    That only occurs if the absolute_range is wider than the observed range in your training data.
    :param my_interval_index:
    :param absolute_range:
    :return:
    """
    interval_list = my_interval_index.to_list()
    # Check if the left boundary needs expanding
    if interval_list[0].left > absolute_range[0]:
        left_outlier = pd.Interval(left=absolute_range[0],
                                   right=interval_list[0].left)
        interval_list.insert(0, left_outlier)

    last = len(interval_list) - 1
    if interval_list[last].right < absolute_range[1]:
        right_outlier = pd.Interval(left=interval_list[last].right,
                                    right=absolute_range[1])
        interval_list.append(right_outlier)
    return pd.IntervalIndex(interval_list)


class HistogramVectorizer(BaseEstimator, TransformerMixin):
    """Convert a time series of binary events into a histogram of
    event occurrences over a time frame. If the data has explicit time stamps
    it can be aggregated over hour of day, day of week, day of month, day of year
    , week of year or month of year."""
    #TODO: time stamps, generic groupby
    def __init__(self,
                 n_bins,
                 strategy='uniform',
                 ground_distance='euclidean',
                 absolute_range=(-np.inf, np.inf),
                 outlier_bins=False):
        """
        :param n_bins: int or array-like, shape (n_features,) (default=5)
            The number of bins to produce. Raises ValueError if n_bins < 2.
        :param strategy: {‘uniform’, ‘quantile’, 'gmm'}, (default=’quantile’)
        :param ground_distance: {'euclidean'}
            The distance to induce between bins.
        :param absolute_range: (minimum_value_possible, maximum_value_possible) (default=(-np.inf, np.inf))
            By default values outside of training data range are included in the extremal bins.
            You can specify these values if you know something about your values (e.g. (0, np.inf) )
        :param outlier_bins: binary (default=False) should I add extra bins to catch values outside of your training
            data where appropriate?
        """
        self._n_bins = n_bins
        self._strategy = strategy
        self._ground_distance = ground_distance
        self._absolute_range= absolute_range
        self._outlier_bins = outlier_bins

    def fit(self, data):
        """
        Learns the histogram bins.
        Still need to check switch.
        :param data:
        :return:
        """
        flat = flatten(data)
        flat = list(filter(lambda n: n>self._absolute_range[0] and n<self._absolute_range[1], flat))
        if (self._strategy=='uniform'):
            self.bin_intervals_ = pd.interval_range(start=np.min(flat), end=np.max(flat), periods=self._n_bins)
        if (self._strategy=='quantile'):
            self.bin_intervals_ = pd.IntervalIndex.from_breaks(find_bin_boundaries(flat, self._n_bins))
        if (self._strategy=='gmm'):
            raise NotImplementedError('Sorry Guassian Mixture model distribution not yet implemented')
        if(self._outlier_bins==True):
            self.bin_intervals_ = add_outier_bins(self.bin_intervals_, self._absolute_range)
        else:
            self.bin_intervals_ = expand_boundaries(self.bin_intervals_, self._absolute_range)
    #    if (len(self._model) != self._n_bins):
    #        print("Warning: Could not find sufficient number of bins. Making do.")
        return self

    def vector_transform(self, vector):
        """
        Applies the transform to a single row of the data.
        """
        return pd.cut(vector, self.bin_intervals_).value_counts()

    def transform(self, data):
        """
        Apply binning to a full data set returning an nparray.
        """
        my_matrix = np.ndarray((len(data), len(self.bin_intervals_)))
        for i, seq in enumerate(data):
            my_matrix[i, :] = self.vector_transform(seq).values
        return my_matrix

    # Need a pandas group by time of day, etc... function

    pass


class SkipgramVectorizer(BaseEstimator, TransformerMixin):
    pass


class NgramVectorizer(BaseEstimator, TransformerMixin):
    pass


class KDEVectorizer(BaseEstimator, TransformerMixin):
    pass


class ProductDistributionVectorizer(BaseEstimator, TransformerMixin):
    pass


class Wasserstein1DHistogramTransformer(BaseEstimator, TransformerMixin):
    pass


class SequentialDifferenceTransformer(BaseEstimator, TransformerMixin):
    pass

