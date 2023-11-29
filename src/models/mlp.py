from __future__ import annotations

# fmt: off
import sys  # isort: skip
from pathlib import Path
from typing import Mapping, Optional, overload

from optuna import Study, Trial

from pandas import DataFrame, Series  # isort: skip
ROOT = Path(__file__).resolve().parent.parent.parent  # isort: skip
sys.path.append(str(ROOT))  # isort: skip
# fmt: on

import matplotlib as mpl

mpl.rcParams["axes.formatter.useoffset"] = False

import os
import sys
from argparse import ArgumentParser, Namespace
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
    no_type_check,
)
from warnings import catch_warnings, simplefilter

import matplotlib.pyplot as plt
import numpy as np
import optuna
import pandas as pd
import torch
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from numpy import ndarray
from optuna import create_study
from optuna.samplers import TPESampler
from pandas import DataFrame, Series
from sklearn.datasets import make_classification
from sklearn.model_selection import KFold, StratifiedKFold
from sklearn.model_selection import cross_validate as cv
from sklearn.svm import SVC, SVR
from skorch import NeuralNetClassifier, NeuralNetRegressor
from skorch.callbacks import EarlyStopping, LRScheduler
from skorch.callbacks.lr_scheduler import CosineAnnealingLR
from torch import Tensor
from torch.nn import (
    BatchNorm1d,
    CrossEntropyLoss,
    Dropout,
    Flatten,
    HuberLoss,
    LazyLinear,
    LeakyReLU,
    Linear,
    Module,
    ModuleList,
    Sequential,
)
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
from tqdm import tqdm
from typing_extensions import Literal

from src._constants import SEED
from src.models.base import NEG_MAE, DfAnalyzeModel

"""
See:

Arlind Kadra, Marius Lindauer, Frank Hutter, Josif Grabocka: “Well-tuned Simple
Nets Excel on Tabular Datasets”, 2021; [http://arxiv.org/abs/2106.11189
arXiv:2106.11189]. https://arxiv.org/pdf/2106.11189.pdf

Fixed Architecture and Optimization Hyperparameters

> In order to focus exclusively on investigating the effect of regularization we
> fix the neural architecture to a simple multilayer perceptron (MLP) and also
> fix some hyperparameters of the general training procedure. These fixed
> hyperparameter values, as specified in Table 4 of Appendix B.1, have been tuned
> for maximizing the performance of an unregularized neural network on our
> dataset collection (see Table 9 in Appendix D). We use a 9-layer feed-forward
> neural network with 512 units for each layer, a choice motivated by previous
> work [42].
>
> Moreover, we set a low learning rate of 10−3 after performing a grid search for
> finding the best value across datasets. We use AdamW [36], which implements
> decoupled weight decay, and cosine annealing with restarts [35] as a learning
> rate scheduler. Using a learning rate scheduler with restarts helps in our case
> because we keep a fixed initial learning rate. For the restarts, we use an
> initial budget of 15 epochs, with a budget multiplier of 2, following published
> practices [62]. Additionally, since our benchmark includes imbalanced datasets,
> we use a weighted version of categorical cross-entropy and balanced accuracy
> [4] as the evaluation metric.

Relevant Details from Table 4 of Fixed hparams:

Table 4: [Fixed Params]

Category                                 Value
......................................   .....
Cosine Annealing Iterations multiplier   2.0
Max. iterations                          15
Activation                               ReLU
Bias initialization                      Yes
Embeddings                               One-Hot encoding
Units in a layer                         512
Preprocessing Preprocessor               None
Batch size                               128
Imputation                               Median
Initialization method                    Default
Learning rate                            1e-3
Loss module                              Weighted Cross-Entropy
Normalization strategy                   Standardize
Optimizer                                AdamW
Scheduler                                Cosine-Annealing w Warm Restarts
Seed                                     11

Table 1 of The paper describes the regularization cocktails: we replicate just
a few, but also tune the LR (they did not find this to be meaningful, likely
given the large amount of regularization options used).

Batch-Norm                  {True, False}
Use Weight-Decay            {True, False}
Weight-Decay                [1e-5, 1e-1]
Use dropout                 {True, False}
Dropout rate                [0.0, 0.8]


"""

NETWORK_MAX_DEPTH = 8
NETWORK_MIN_DEPTH = 3
NETWORK_MAX_WIDTH = 512
NETWORK_MIN_WIDTH = 32
BATCH_SIZE = 128

torch.set_num_threads(1)
torch.set_num_interop_threads(1)


class SkorchMLP(Module):
    def __init__(
        self,
        width: int = NETWORK_MAX_WIDTH,
        depth: int = NETWORK_MAX_DEPTH,
        use_bn: bool = True,
        dropout: float = 0.4,
        num_classes: int = 2,
    ) -> None:
        super().__init__()
        self.hparams = dict(
            width=width,
            use_bn=use_bn,
            dropout=dropout,
        )
        self.num_classes = num_classes
        self.is_classification = num_classes >= 2
        self.out_channels = num_classes

        W = width
        D = dropout if dropout > 0.0 else 0.0
        self.input = LazyLinear(out_features=width)
        self.layers = ModuleList()
        for i in range(depth):
            self.layers.append(Linear(W, W, bias=not use_bn))
            if use_bn:
                self.layers.append(BatchNorm1d(W))
            if depth - 1:
                self.layers.append(Dropout(D))
            self.layers.append(LeakyReLU())

        self.backbone = Sequential(*self.layers)
        self.output = Linear(W, self.out_channels, bias=True)

    def forward(self, x: Tensor) -> Tensor:
        x = self.input(x)
        x = self.backbone(x)
        x = self.output(x)
        if self.is_classification:
            return x
        return torch.flatten(x)


def get_T0(
    X: Union[ndarray, DataFrame],
    n_epochs: int,
    batch_size: int = BATCH_SIZE,
    val_split: Optional[Union[int, float]] = None,
) -> tuple[int, int]:
    N = len(X)
    if val_split is None:
        n_batches = N // batch_size
    elif isinstance(val_split, int):
        n_batches = (N - val_split) // batch_size
    elif isinstance(val_split, float) and (0.0 < val_split < 1.0):
        n_batches = int(N * val_split) // batch_size
    else:
        raise ValueError("Invalid `val_split` argument.")
    if n_batches == 0:
        return n_epochs, n_batches

    return n_epochs * n_batches, n_batches


class MLPEstimator(DfAnalyzeModel):
    def __init__(self, num_classes: int, model_args: Mapping | None = None) -> None:
        super().__init__(model_args)
        self.is_classifier = num_classes > 1
        self.model_cls = NeuralNetClassifier if self.is_classifier else NeuralNetRegressor
        self.model: Union[NeuralNetClassifier, NeuralNetRegressor]
        self.fixed_args = dict(
            module=SkorchMLP,
            module__num_classes=num_classes,
            criterion=CrossEntropyLoss if self.is_classifier else HuberLoss,
            optimizer=AdamW,
            max_epochs=50,
            batch_size=BATCH_SIZE,
            # iterator_train__num_workers=1,  # for some reason causes huge slow
            device="cpu",
            verbose=0,
        )

    def optuna_args(self, trial: Trial) -> dict[str, str | float | int]:
        widths = (16, 32, 64, 128, 256, 512)
        bools = (True, False)
        return dict(
            # SkorchMLP args
            module__width=trial.suggest_categorical("module__width", widths),
            module__depth=trial.suggest_int("module__depth", 3, 8),
            module__use_bn=trial.suggest_categorical("module__use_bn", bools),
            module__dropout=trial.suggest_float("module__dropout", 0.0, 0.7, step=0.1),
            # NeuralNet[Estimator] args
            early_stopping=trial.suggest_categorical("early_stopping", bools),
            restarts=trial.suggest_categorical("restarts", bools),
            optimizer__lr=trial.suggest_float("optimizer__lr", 1e-5, 1e-1, log=True),
            optimizer__weight_decay=trial.suggest_float(
                "optimizer__weight_decay", 1e-7, 1e-1, log=True
            ),
        )

    def _get_scheduler(
        self, X_train: DataFrame, restarts: bool, val_split: bool
    ) -> Union[CosineAnnealingWarmRestarts, CosineAnnealingLR]:
        cls = CosineAnnealingWarmRestarts if restarts else CosineAnnealingLR
        split = 0.2 if val_split else None
        n_epochs = 8 if restarts else 50  # "period" of the annealing
        period = get_T0(X_train, n_epochs=n_epochs, val_split=split)[0]
        shared: Mapping = dict(eta_min=0, verbose=False, step_every="step")
        return (
            LRScheduler(policy=cls, T_0=period, T_mult=2, **shared)  # type: ignore
            if restarts
            else LRScheduler(policy=cls, T_max=period, **shared)  # type: ignore
        )

    @overload
    def _to_torch(self, X: DataFrame) -> Tensor:
        ...

    @overload
    def _to_torch(self, X: DataFrame, y: Series) -> tuple[Tensor, Tensor]:
        ...

    def _to_torch(
        self, X: DataFrame, y: Optional[Series] = None
    ) -> Union[tuple[Tensor, Tensor], Tensor]:
        Xt = torch.from_numpy(X.to_numpy()).to(dtype=torch.float32)
        if y is None:
            return Xt

        if self.is_classifier:
            yt = torch.from_numpy(y.to_numpy()).to(dtype=torch.int64)
        else:
            yt = torch.from_numpy(y.to_numpy()).to(dtype=torch.float32)
        return Xt, yt

    def fit(self, X_train: DataFrame, y_train: Series) -> None:
        if self.model is None:
            kwargs = {**self.fixed_args, **self.default_args, **self.model_args}
            self.model = self.model_cls(**kwargs)
        X, y = self._to_torch(X_train, y_train)
        self.model.fit(X, y)

    def refit(self, X: DataFrame, y: Series, overrides: Optional[Mapping] = None) -> None:
        overrides = overrides or {}
        kwargs = {
            **self.fixed_args,
            **self.default_args,
            **self.model_args,
            **overrides,
        }
        self.model = self.model_cls(**kwargs)
        Xt, yt = self._to_torch(X, y)
        self.model.fit(Xt, yt)

    def predict(self, X: DataFrame) -> ndarray:
        Xt = self._to_torch(X)
        return self.model.predict(Xt)

    def predict_proba(self, X: DataFrame) -> ndarray:
        Xt = self._to_torch(X)
        return self.model.predict_proba(Xt)

    def score(self, X: DataFrame, y: Series) -> float:
        Xt, yt = self._to_torch(X, y)
        if self.model is None:
            raise RuntimeError("Need to call `model.fit()` before calling `.score()`")
        return float(self.model.score(Xt, yt))

    def _to_model_args(self, optuna_args: dict[str, Any], X_train: DataFrame) -> dict[str, Any]:
        final_args: dict[str, Any] = deepcopy(optuna_args)
        restarts = final_args.pop("restarts")
        early = final_args.pop("early_stopping")

        callbacks: list[Any] = [EarlyStopping(patience=20)] if early else []
        sched = self._get_scheduler(X_train, restarts=restarts, val_split=early)
        callbacks.append(sched)

        final_args["callbacks"] = callbacks
        return final_args

    def optuna_objective(
        self, X_train: DataFrame, y_train: Series, n_folds: int = 3
    ) -> Callable[[Trial], float]:
        X, y = self._to_torch(X_train, y_train)

        def objective(trial: Trial) -> float:
            raw_args = self.optuna_args(trial)

            final_args = self._to_model_args(raw_args, X_train)
            args = {**self.fixed_args, **self.default_args, **final_args}
            estimator = self.model_cls(**args)

            scoring = "accuracy" if self.is_classifier else NEG_MAE
            kf = StratifiedKFold if self.is_classifier else KFold
            # _cv = kf(n_splits=n_folds, shuffle=True, random_state=SEED)
            _cv = kf(n_splits=5, shuffle=True, random_state=SEED)
            # TODO: maybe use NeuralNet class cv option?
            # TODO: maybe not use k-fold here due to costs, or e.g. 2-fold
            idx_tr, idx_test = next(_cv.split(y, y))
            X_tr, X_test = X[idx_tr], X[idx_test]
            y_tr, y_test = y[idx_tr], y[idx_test]
            estimator.fit(X_tr, y_tr)
            score = estimator.score(X_test, y_test)
            return -float(score)
            # scores = cv(
            #     estimator,  # type: ignore
            #     X=X,
            #     y=y,
            #     scoring=scoring,
            #     cv=_cv,
            #     n_jobs=1,
            #     verbose=1,
            # )
            return float(np.mean(scores["test_score"]))

        return objective

    def htune_eval(
        self,
        X_train: DataFrame,
        y_train: Series,
        X_test: DataFrame,
        y_test: Series,
    ) -> Any:
        # TODO: need to specify valiation method, and return confidences, etc.
        # Actually maybe just want to call refit in here...
        if self.tuned_args is None:
            raise RuntimeError("Cannot evaluate tuning because model has not been tuned.")

        args = self._to_model_args(self.tuned_args, X_train)
        self.refit(X_train, y_train, overrides=args)
        # TODO: return Platt-scaling or probability estimates
        return self.score(X_test, y_test)


if __name__ == "__main__":
    X, y = make_classification(2000, 20, n_informative=10, random_state=0)
    X = X.astype(np.float32)
    y = y.astype(np.int64)
    X_train = X[:1000]
    y_train = y[:1000]
    X_test = X[1000:]
    y_test = y[1000:]
    wd = 1e-4
    lr = 1e-3
    # for lr in tqdm([1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2]):
    for lr in tqdm([1e-4]):
        T0, n_batches = get_T0(X, n_epochs=8, val_split=0.2)
        sched = LRScheduler(
            policy=CosineAnnealingWarmRestarts,  # type: ignore
            T_0=T0,
            T_mult=2,
            eta_min=0,
            verbose=False,
            step_every="step",
        )
        T0, n_batches = get_T0(X, n_epochs=50, val_split=0.2)
        sched = LRScheduler(
            policy=CosineAnnealingLR,  # type: ignore
            # T_max=T0,
            T_max=T0,
            eta_min=0,
            verbose=True,
            step_every="step",
        )
        # lrs = sched.simulate(steps=50 * n_batches, initial_lr=lr)
        # epochs = [int(i / n_batches) for i in range(len(lrs))]
        # ax: Axes
        # fig, ax = plt.subplots()
        # ax.plot(epochs, lrs, color="black")
        # ax.set_xlabel("Epoch")
        # ax.set_ylabel("LR")
        # ax.ticklabel_format(axis="y", style="sci", scilimits=(0, 0), useOffset=False)
        # ax.set_yscale("log")
        # plt.show()
        # plt.close()
        # sys.exit()

        with catch_warnings():
            simplefilter("ignore", UserWarning)
            net = NeuralNetClassifier(
                module=SkorchMLP,
                module__width=128,
                module__use_bn=True,
                module__use_drop=True,
                module__lr=lr,
                module__wd=wd,
                module__dropout=0.4,
                module__num_classes=2,
                criterion=CrossEntropyLoss,  # type: ignore
                criterion__weight=None,
                optimizer=AdamW,
                optimizer__weight_decay=wd,
                optimizer__lr=lr,
                callbacks=[
                    sched,
                    # Effectively min_epochs=20
                    EarlyStopping(patience=20, load_best=False),
                ],
                max_epochs=50,
                batch_size=BATCH_SIZE,
                # train_split=None,  # we do all tuning with 5-fold anyway...
                device="cpu",
                verbose=0,
            )
            net.fit(X_train, y_train)
            # probs = net.predict_proba(X_test)
            # print(probs[:10])
            # preds = net.predict(X_test)
            # print(preds[:10])
            print("Acc:", net.score(X_test, y_test))
            fig, ax = plt.subplots()
            ax.plot(net.history[:, "train_loss"], color="black", label="train")
            ax.plot(net.history[:, "valid_loss"], color="orange", label="val")
            ax.set_title(f"LR={lr}")
            plt.show(block=False)
    plt.show()
