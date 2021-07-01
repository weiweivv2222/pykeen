# -*- coding: utf-8 -*-

"""Non-parametric baselines."""

import itertools as itt
import time
from abc import ABC
from functools import partial
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Type, cast

import click
import matplotlib.pyplot as plt
import numpy
import pandas as pd
import scipy.sparse
import seaborn as sns
import torch
from more_click import verbose_option
from sklearn.preprocessing import normalize as sklearn_normalize
from tabulate import tabulate
from tqdm import trange
from tqdm.contrib.concurrent import process_map
from tqdm.contrib.logging import logging_redirect_tqdm

from pykeen.constants import PYKEEN_EXPERIMENTS
from pykeen.datasets import Dataset, dataset_resolver
from pykeen.evaluation import RankBasedEvaluator, RankBasedMetricResults, evaluate
from pykeen.models import Model
from pykeen.triples import CoreTriplesFactory

BENCHMARK_PATH = PYKEEN_EXPERIMENTS.joinpath('baseline_benchmark.tsv')
TEST_BENCHMARK_PATH = PYKEEN_EXPERIMENTS.joinpath('baseline_benchmark_test.tsv')
KS = (1, 5, 10, 50, 100)
METRICS = ['mrr', 'iamr', 'igmr', *(f'hits@{k}' for k in KS), 'aamr', 'aamri']


def _get_max_id(triples_factory: CoreTriplesFactory, index: int) -> int:
    """Get the number of entities or relations, depending on the selected column index."""
    return triples_factory.num_relations if index == 1 else triples_factory.num_entities


def _get_csr_matrix(
    triples_factory: CoreTriplesFactory,
    col_index: int,
    row_index: int,
    normalize: bool = False,
) -> scipy.sparse.csr_matrix:
    """Create a co-occurrence matrix from triples."""
    assert row_index != col_index
    row, col = triples_factory.mapped_triples.T[[row_index, col_index]]
    num_rows = _get_max_id(triples_factory=triples_factory, index=row_index)
    num_columns = _get_max_id(triples_factory=triples_factory, index=col_index)
    matrix = scipy.sparse.coo_matrix(
        (numpy.ones(triples_factory.num_triples), (row, col)),
        shape=(num_rows, num_columns),
    ).tocsr()
    if normalize:
        matrix = sklearn_normalize(matrix, norm="l1", axis=1)
    return matrix


class EvaluationOnlyModel(Model, ABC):
    """A model which only implements the methods used for evaluation."""

    def _reset_parameters_(self):
        # TODO: this is not needed for non-parametric models!
        raise NotImplementedError

    def collect_regularization_term(self) -> torch.FloatTensor:  # noqa:D102
        # TODO: this is not needed for non-parametric models!
        raise NotImplementedError

    def score_hrt(self, hrt_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa:D102
        # TODO: this is not needed for evaluation
        raise NotImplementedError

    def score_r(self, ht_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa:D102
        # TODO: this is not needed for evaluation
        raise NotImplementedError


class PseudoTypeBaseline(EvaluationOnlyModel):
    """Score based on entity-relation co-occurrence."""

    def __init__(
        self,
        triples_factory: CoreTriplesFactory,
        normalize: bool = False,
    ):
        super().__init__(triples_factory=triples_factory, random_seed=0, preferred_device='cpu')
        self.head_per_relation = _get_csr_matrix(
            triples_factory=triples_factory, row_index=1, col_index=0, normalize=normalize,
        )
        self.tail_per_relation = _get_csr_matrix(
            triples_factory=triples_factory, row_index=1, col_index=2, normalize=normalize,
        )
        self.normalize = normalize

    def score_t(self, hr_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa:D102
        r = hr_batch[:, 1].cpu().numpy()
        return torch.from_numpy(self.tail_per_relation[r].todense())

    def score_h(self, rt_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa:D102
        r = rt_batch[:, 0].cpu().numpy()
        return torch.from_numpy(self.head_per_relation[r].todense())


class EntityCoOccurrenceBaseline(EvaluationOnlyModel):
    """Score based on entity-entity co-occurrence."""

    def __init__(
        self,
        triples_factory: CoreTriplesFactory,
        normalize: bool = False,
    ):
        super().__init__(triples_factory=triples_factory, random_seed=0, preferred_device='cpu')
        self.head_per_tail = _get_csr_matrix(
            triples_factory=triples_factory, row_index=2, col_index=0, normalize=normalize,
        )
        self.tail_per_head = _get_csr_matrix(
            triples_factory=triples_factory, row_index=0, col_index=2, normalize=normalize,
        )
        self.normalize = normalize

    def score_t(self, hr_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa:D102
        h = hr_batch[:, 0].cpu().numpy()
        return torch.from_numpy(self.tail_per_head[h].todense())

    def score_h(self, rt_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa:D102
        t = rt_batch[:, 1].cpu().numpy()
        return torch.from_numpy(self.head_per_tail[t].todense())


def _get_relation_similarity(
    triples_factory: CoreTriplesFactory,
    to_inverse: bool = False,
    threshold: Optional[float] = None,
) -> scipy.sparse.csr_matrix:
    # TODO: overlap with inverse triple detection
    assert triples_factory.num_entities * triples_factory.num_relations < numpy.iinfo(int_type=int).max
    mapped_triples = numpy.asarray(triples_factory.mapped_triples)
    r = scipy.sparse.coo_matrix(
        (
            numpy.ones((mapped_triples.shape[0],), dtype=int),
            (
                mapped_triples[:, 1],
                triples_factory.num_entities * mapped_triples[:, 0] + mapped_triples[:, 2],
            ),
        ),
        shape=(triples_factory.num_relations, triples_factory.num_entities ** 2),
    )
    cardinality = numpy.asarray(r.sum(axis=1)).squeeze(axis=-1)
    if to_inverse:
        r2 = scipy.sparse.coo_matrix(
            (
                numpy.ones((mapped_triples.shape[0],), dtype=int),
                (
                    mapped_triples[:, 1],
                    triples_factory.num_entities * mapped_triples[:, 2] + mapped_triples[:, 0],
                ),
            ),
            shape=(triples_factory.num_relations, triples_factory.num_entities ** 2),
        )
    else:
        r2 = r
    intersection = numpy.asarray((r @ r2.T).todense())
    union = cardinality[:, None] + cardinality[None, :] - intersection
    sim = intersection.astype(numpy.float32) / union.astype(numpy.float32)
    if threshold is not None:
        sim[sim < threshold] = 0.0
    sim = scipy.sparse.csr_matrix(sim)
    sim.eliminate_zeros()
    return sim


class SoftInverseTripleBaseline(EvaluationOnlyModel):
    """Score based on relation similarity."""

    def __init__(
        self,
        triples_factory: CoreTriplesFactory,
        threshold: Optional[float] = None,
    ):
        super().__init__(triples_factory=triples_factory, random_seed=0, preferred_device='cpu')
        # compute relation similarity matrix
        self.sim = _get_relation_similarity(triples_factory, to_inverse=False, threshold=threshold)
        self.sim_inv = _get_relation_similarity(triples_factory, to_inverse=True, threshold=threshold)

        mapped_triples = numpy.asarray(triples_factory.mapped_triples)
        self.rel_to_head = scipy.sparse.coo_matrix(
            (
                numpy.ones(shape=(triples_factory.num_triples,), dtype=numpy.float32),
                (mapped_triples[:, 1], mapped_triples[:, 0]),
            ),
            shape=(triples_factory.num_relations, triples_factory.num_entities),
        ).tocsr()
        self.rel_to_tail = scipy.sparse.coo_matrix(
            (
                numpy.ones(shape=(triples_factory.num_triples,), dtype=numpy.float32),
                (mapped_triples[:, 1], mapped_triples[:, 2]),
            ),
            shape=(triples_factory.num_relations, triples_factory.num_entities),
        ).tocsr()

    def score_t(self, hr_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa:D102
        r = hr_batch[:, 1]
        scores = self.sim[r, :] @ self.rel_to_tail + self.sim_inv[r, :] @ self.rel_to_head
        scores = numpy.asarray(scores.todense())
        return torch.from_numpy(scores)

    def score_h(self, rt_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa:D102
        r = rt_batch[:, 0]
        scores = self.sim[r, :] @ self.rel_to_head + self.sim_inv[r, :] @ self.rel_to_tail
        scores = numpy.asarray(scores.todense())
        return torch.from_numpy(scores)


@click.command()
@verbose_option
@click.option('--batch-size', default=2048, show_default=True)
@click.option('--trials', default=10, show_default=True)
@click.option('--rebuild', is_flag=True)
@click.option('--test', is_flag=True)
@click.option('--path')
def main(batch_size: int, trials: int, rebuild: bool, test: bool, path):
    """Run the baseline showcase."""
    if path is None:
        path = TEST_BENCHMARK_PATH if test else BENCHMARK_PATH
    path = Path(path)
    if not path.is_file() or rebuild or test:
        with logging_redirect_tqdm():
            df = _build(batch_size=batch_size, trials=trials, path=path, test=test)
    else:
        df = pd.read_csv(path, sep='\t')

    _plot(df, test=test)


def _melt(df: pd.DataFrame) -> pd.DataFrame:
    keep = [col for col in df.columns if col not in METRICS]
    return pd.melt(
        df[[*keep, *METRICS]],
        id_vars=keep,
        value_vars=METRICS,
        var_name='metric',
    )


def _plot(df: pd.DataFrame, skip_small: bool = True, test: bool = False):
    if skip_small and not test:
        df = df[~df.dataset.isin({'Nations', 'Countries', 'UMLS', 'Kinships'})]
    tsdf = _melt(df)

    # Plot relation between dataset and time, stratified by model
    # Interpretation: exponential relationship between # triples and time
    g = sns.catplot(
        data=tsdf,
        y='dataset',
        x='time',
        hue='model',
        kind='box',
        aspect=1.5,
    ).set(xscale='log', xlabel='Time (seconds)', ylabel='')
    g.fig.savefig(PYKEEN_EXPERIMENTS.joinpath('baseline_benchmark_timeplot.svg'))
    g.fig.savefig(PYKEEN_EXPERIMENTS.joinpath('baseline_benchmark_timeplot.png'), dpi=300)
    plt.close(g.fig)

    # Show AMRI plots. Surprisingly, some performance is really good.
    g = sns.catplot(
        data=tsdf[tsdf.metric == 'aamri'],
        y='dataset',
        x='value',
        hue='model',
        kind='violin',
        aspect=1.5,
    ).set(xlabel='Adjusted Mean Rank Index', ylabel='')
    g.fig.savefig(PYKEEN_EXPERIMENTS.joinpath('baseline_benchmark_aamri.svg'))
    g.fig.savefig(PYKEEN_EXPERIMENTS.joinpath('baseline_benchmark_aamri.png'), dpi=300)
    plt.close(g.fig)

    # Make a violinplot grid showing relation between # triples and result, stratified by model and metric.
    # Interpretation: no dataset size dependence
    g = sns.catplot(
        data=tsdf[~tsdf.metric.isin({'aamr', 'aamri'})],
        y='dataset',
        x='value',
        hue='model',
        col='metric',
        kind="violin",
        col_wrap=2,
        height=0.5 * tsdf['dataset'].nunique(),
        aspect=1.5,
    )
    g.set(ylabel='')
    g.fig.savefig(PYKEEN_EXPERIMENTS.joinpath('baseline_benchmark_scatterplot.svg'))
    g.fig.savefig(PYKEEN_EXPERIMENTS.joinpath('baseline_benchmark_scatterplot.png'), dpi=300)
    plt.close(g.fig)


def _build(batch_size: int, trials: int, path: Union[str, Path], test: bool = False) -> pd.DataFrame:
    datasets = sorted(dataset_resolver, key=Dataset.triples_sort_key)
    if test:
        datasets = datasets[:4]
    else:
        # FB15K and CoDEx Large are the first datasets where this gets a bit out of hand
        datasets = datasets[:1 + datasets.index(dataset_resolver.lookup('FB15k'))]
    models_kwargs: List[Tuple[Type[EvaluationOnlyModel], Mapping[str, Any]]] = [
        (PseudoTypeBaseline, dict(normalize=True)),
        (EntityCoOccurrenceBaseline, dict(normalize=True)),
        (SoftInverseTripleBaseline, dict(threshold=0.97)),
    ]
    kwargs_keys = sorted({k for _, d in models_kwargs for k in d})

    it = process_map(
        partial(
            _run_trials,
            batch_size=batch_size,
            kwargs_keys=kwargs_keys,
            trials=trials,
        ),
        itt.product(datasets, models_kwargs),
        desc='Baseline',
        total=len(datasets) * len(models_kwargs),
    )
    rows = list(itt.chain.from_iterable(it))
    columns = ['dataset', 'entities', 'relations', 'triples', 'trial', 'model', *kwargs_keys, 'time', *METRICS]
    df = pd.DataFrame(rows, columns=columns)
    df.to_csv(path, sep='\t', index=False)
    print(tabulate(df.round(3).values, headers=columns, tablefmt='github'))
    return df


def _run_trials(
    t: Tuple[Type[Dataset], Tuple[Type[Model], Mapping[str, Any]]],
    *,
    trials: int,
    batch_size: int,
    kwargs_keys: Sequence[str],
) -> List[Tuple[Any, ...]]:
    dataset_cls, (model_cls, model_kwargs) = t

    model_name = model_cls.__name__[:-len('Baseline')]
    dataset_name = dataset_cls.__name__
    dataset = dataset_cls()
    base_record = (
        dataset_name,
        dataset.training.num_entities,
        dataset.training.num_relations,
        dataset.training.num_triples,
    )
    records = []
    for trial in trange(trials, leave=False, desc=f'{dataset_name}/{model_name}'):
        if trials != 0:
            trial_dataset = dataset.remix(random_state=trial)
        else:
            trial_dataset = dataset
        model = model_cls(triples_factory=trial_dataset.training, **model_kwargs)

        start_time = time.time()
        result = _evaluate_baseline(trial_dataset, model, batch_size=batch_size)
        elapsed_seconds = time.time() - start_time

        records.append((
            *base_record,
            trial,
            model_name,
            *(model_kwargs.get(key) for key in kwargs_keys),
            elapsed_seconds,
            *(result.get_metric(metric) for metric in METRICS),
        ))
    return records


def _evaluate_baseline(dataset: Dataset, model: Model, batch_size=None) -> RankBasedMetricResults:
    assert dataset.validation is not None
    evaluator = RankBasedEvaluator(ks=KS)
    return cast(RankBasedMetricResults, evaluate(
        model=model,
        mapped_triples=dataset.testing.mapped_triples,
        evaluators=evaluator,
        batch_size=batch_size,
        additional_filter_triples=[
            dataset.training.mapped_triples,
            dataset.validation.mapped_triples,
        ],
        use_tqdm=100_000 < dataset.training.num_triples,  # only use for big datasets
    ))


if __name__ == '__main__':
    main()
