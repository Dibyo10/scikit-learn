"""Gaussian Mixture Model."""

# Authors: The scikit-learn developers
# SPDX-License-Identifier: BSD-3-Clause
import math

import numpy as np

from sklearn._config import get_config
from sklearn.externals import array_api_extra as xpx
from sklearn.mixture._base import BaseMixture, _check_shape
from sklearn.utils import check_array
from sklearn.utils._array_api import (
    _add_to_diagonal,
    _cholesky,
    _linalg_solve,
    get_namespace,
    get_namespace_and_device,
)
from sklearn.utils._param_validation import StrOptions
from sklearn.utils.extmath import row_norms

###############################################################################
# Gaussian mixture shape checkers used by the GaussianMixture class


def _check_weights(weights, n_components, xp=None):
    """Check the user provided 'weights'.

    Parameters
    ----------
    weights : array-like of shape (n_components,)
        The proportions of components of each mixture.

    n_components : int
        Number of components.

    Returns
    -------
    weights : array, shape (n_components,)
    """
    weights = check_array(weights, dtype=[xp.float64, xp.float32], ensure_2d=False)
    _check_shape(weights, (n_components,), "weights")

    # check range
    if any(xp.less(weights, 0.0)) or any(xp.greater(weights, 1.0)):
        raise ValueError(
            "The parameter 'weights' should be in the range "
            "[0, 1], but got max value %.5f, min value %.5f"
            % (xp.min(weights), xp.max(weights))
        )

    # check normalization
    atol = 1e-6 if weights.dtype == xp.float32 else 1e-8
    if not np.allclose(float(xp.abs(1.0 - xp.sum(weights))), 0.0, atol=atol):
        raise ValueError(
            "The parameter 'weights' should be normalized, but got sum(weights) = %.5f"
            % xp.sum(weights)
        )
    return weights


def _check_means(means, n_components, n_features, xp=None):
    """Validate the provided 'means'.

    Parameters
    ----------
    means : array-like of shape (n_components, n_features)
        The centers of the current components.

    n_components : int
        Number of components.

    n_features : int
        Number of features.

    Returns
    -------
    means : array, (n_components, n_features)
    """
    xp, _ = get_namespace(means, xp=xp)
    means = check_array(means, dtype=[xp.float64, xp.float32], ensure_2d=False)
    _check_shape(means, (n_components, n_features), "means")
    return means


def _check_precision_positivity(precision, covariance_type, xp=None):
    """Check a precision vector is positive-definite."""
    xp, _ = get_namespace(precision, xp=xp)
    if xp.any(xp.less_equal(precision, 0.0)):
        raise ValueError("'%s precision' should be positive" % covariance_type)


def _check_precision_matrix(precision, covariance_type, xp=None):
    """Check a precision matrix is symmetric and positive-definite."""
    xp, _ = get_namespace(precision, xp=xp)
    if not (
        xp.all(xpx.isclose(precision, precision.T))
        and xp.all(xp.linalg.eigvalsh(precision) > 0.0)
    ):
        raise ValueError(
            "'%s precision' should be symmetric, positive-definite" % covariance_type
        )


def _check_precisions_full(precisions, covariance_type, xp=None):
    """Check the precision matrices are symmetric and positive-definite."""
    xp, _ = get_namespace(precisions, xp=xp)
    for i in range(precisions.shape[0]):
        _check_precision_matrix(precisions[i, :, :], covariance_type, xp=xp)


def _check_precisions(precisions, covariance_type, n_components, n_features, xp=None):
    """Validate user provided precisions.

    Parameters
    ----------
    precisions : array-like
        'full' : shape of (n_components, n_features, n_features)
        'tied' : shape of (n_features, n_features)
        'diag' : shape of (n_components, n_features)
        'spherical' : shape of (n_components,)

    covariance_type : str

    n_components : int
        Number of components.

    n_features : int
        Number of features.

    Returns
    -------
    precisions : array
    """
    xp, _ = get_namespace(precisions, xp=xp)
    precisions = check_array(
        precisions,
        dtype=[xp.float64, xp.float32],
        ensure_2d=False,
        allow_nd=covariance_type == "full",
    )

    precisions_shape = {
        "full": (n_components, n_features, n_features),
        "tied": (n_features, n_features),
        "diag": (n_components, n_features),
        "spherical": (n_components,),
    }
    _check_shape(
        precisions, precisions_shape[covariance_type], "%s precision" % covariance_type
    )

    _check_precisions = {
        "full": _check_precisions_full,
        "tied": _check_precision_matrix,
        "diag": _check_precision_positivity,
        "spherical": _check_precision_positivity,
    }
    _check_precisions[covariance_type](precisions, covariance_type, xp=xp)
    return precisions


###############################################################################
# Gaussian mixture parameters estimators (used by the M-Step)


def _estimate_gaussian_covariances_full(resp, X, nk, means, reg_covar, xp=None):
    """Estimate the full covariance matrices.

    Parameters
    ----------
    resp : array-like of shape (n_samples, n_components)

    X : array-like of shape (n_samples, n_features)

    nk : array-like of shape (n_components,)

    means : array-like of shape (n_components, n_features)

    reg_covar : float

    Returns
    -------
    covariances : array, shape (n_components, n_features, n_features)
        The covariance matrix of the current components.
    """
    xp, _, device_ = get_namespace_and_device(X, xp=xp)
    n_components, n_features = means.shape
    covariances = xp.empty(
        (n_components, n_features, n_features), device=device_, dtype=X.dtype
    )
    for k in range(n_components):
        diff = X - means[k, :]
        covariances[k, :, :] = ((resp[:, k] * diff.T) @ diff) / nk[k]
        _add_to_diagonal(covariances[k, :, :], reg_covar, xp)
    return covariances


def _estimate_gaussian_covariances_tied(resp, X, nk, means, reg_covar, xp=None):
    """Estimate the tied covariance matrix.

    Parameters
    ----------
    resp : array-like of shape (n_samples, n_components)

    X : array-like of shape (n_samples, n_features)

    nk : array-like of shape (n_components,)

    means : array-like of shape (n_components, n_features)

    reg_covar : float

    Returns
    -------
    covariance : array, shape (n_features, n_features)
        The tied covariance matrix of the components.
    """
    xp, _ = get_namespace(X, means, xp=xp)
    avg_X2 = X.T @ X
    avg_means2 = nk * means.T @ means
    covariance = avg_X2 - avg_means2
    covariance /= xp.sum(nk)
    _add_to_diagonal(covariance, reg_covar, xp)
    return covariance


def _estimate_gaussian_covariances_diag(resp, X, nk, means, reg_covar, xp=None):
    """Estimate the diagonal covariance vectors.

    Parameters
    ----------
    responsibilities : array-like of shape (n_samples, n_components)

    X : array-like of shape (n_samples, n_features)

    nk : array-like of shape (n_components,)

    means : array-like of shape (n_components, n_features)

    reg_covar : float

    Returns
    -------
    covariances : array, shape (n_components, n_features)
        The covariance vector of the current components.
    """
    xp, _ = get_namespace(X, xp=xp)
    avg_X2 = (resp.T @ (X * X)) / nk[:, xp.newaxis]
    avg_means2 = means**2
    return avg_X2 - avg_means2 + reg_covar


def _estimate_gaussian_covariances_spherical(resp, X, nk, means, reg_covar, xp=None):
    """Estimate the spherical variance values.

    Parameters
    ----------
    responsibilities : array-like of shape (n_samples, n_components)

    X : array-like of shape (n_samples, n_features)

    nk : array-like of shape (n_components,)

    means : array-like of shape (n_components, n_features)

    reg_covar : float

    Returns
    -------
    variances : array, shape (n_components,)
        The variance values of each components.
    """
    xp, _ = get_namespace(X)
    return xp.mean(
        _estimate_gaussian_covariances_diag(resp, X, nk, means, reg_covar, xp=xp),
        axis=1,
    )


def _estimate_gaussian_parameters(X, resp, reg_covar, covariance_type, xp=None):
    """Estimate the Gaussian distribution parameters.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        The input data array.

    resp : array-like of shape (n_samples, n_components)
        The responsibilities for each data sample in X.

    reg_covar : float
        The regularization added to the diagonal of the covariance matrices.

    covariance_type : {'full', 'tied', 'diag', 'spherical'}
        The type of precision matrices.

    Returns
    -------
    nk : array-like of shape (n_components,)
        The numbers of data samples in the current components.

    means : array-like of shape (n_components, n_features)
        The centers of the current components.

    covariances : array-like
        The covariance matrix of the current components.
        The shape depends of the covariance_type.
    """
    xp, _ = get_namespace(X, xp=xp)
    nk = xp.sum(resp, axis=0) + 10 * xp.finfo(resp.dtype).eps
    means = (resp.T @ X) / nk[:, xp.newaxis]
    covariances = {
        "full": _estimate_gaussian_covariances_full,
        "tied": _estimate_gaussian_covariances_tied,
        "diag": _estimate_gaussian_covariances_diag,
        "spherical": _estimate_gaussian_covariances_spherical,
    }[covariance_type](resp, X, nk, means, reg_covar, xp=xp)
    return nk, means, covariances


def _compute_precision_cholesky(covariances, covariance_type, xp=None):
    """Compute the Cholesky decomposition of the precisions.

    Parameters
    ----------
    covariances : array-like
        The covariance matrix of the current components.
        The shape depends of the covariance_type.

    covariance_type : {'full', 'tied', 'diag', 'spherical'}
        The type of precision matrices.

    Returns
    -------
    precisions_cholesky : array-like
        The cholesky decomposition of sample precisions of the current
        components. The shape depends of the covariance_type.
    """
    xp, _, device_ = get_namespace_and_device(covariances, xp=xp)

    estimate_precision_error_message = (
        "Fitting the mixture model failed because some components have "
        "ill-defined empirical covariance (for instance caused by singleton "
        "or collapsed samples). Try to decrease the number of components, "
        "increase reg_covar, or scale the input data."
    )
    dtype = covariances.dtype
    if dtype == xp.float32:
        estimate_precision_error_message += (
            " The numerical accuracy can also be improved by passing float64"
            " data instead of float32."
        )

    if covariance_type == "full":
        n_components, n_features, _ = covariances.shape
        precisions_chol = xp.empty(
            (n_components, n_features, n_features), device=device_, dtype=dtype
        )
        for k in range(covariances.shape[0]):
            covariance = covariances[k, :, :]
            try:
                cov_chol = _cholesky(covariance, xp)
            # catch only numpy exceptions, b/c exceptions aren't part of array api spec
            except np.linalg.LinAlgError:
                raise ValueError(estimate_precision_error_message)
            precisions_chol[k, :, :] = _linalg_solve(
                cov_chol, xp.eye(n_features, dtype=dtype, device=device_), xp
            ).T
    elif covariance_type == "tied":
        _, n_features = covariances.shape
        try:
            cov_chol = _cholesky(covariances, xp)
        # catch only numpy exceptions, since exceptions are not part of array api spec
        except np.linalg.LinAlgError:
            raise ValueError(estimate_precision_error_message)
        precisions_chol = _linalg_solve(
            cov_chol, xp.eye(n_features, dtype=dtype, device=device_), xp
        ).T
    else:
        if xp.any(covariances <= 0.0):
            raise ValueError(estimate_precision_error_message)
        precisions_chol = 1.0 / xp.sqrt(covariances)
    return precisions_chol


def _flipudlr(array, xp=None):
    """Reverse the rows and columns of an array."""
    xp, _ = get_namespace(array, xp=xp)
    return xp.flip(xp.flip(array, axis=1), axis=0)


def _compute_precision_cholesky_from_precisions(precisions, covariance_type, xp=None):
    r"""Compute the Cholesky decomposition of precisions using precisions themselves.

    As implemented in :func:`_compute_precision_cholesky`, the `precisions_cholesky_` is
    an upper-triangular matrix for each Gaussian component, which can be expressed as
    the $UU^T$ factorization of the precision matrix for each Gaussian component, where
    $U$ is an upper-triangular matrix.

    In order to use the Cholesky decomposition to get $UU^T$, the precision matrix
    $\Lambda$ needs to be permutated such that its rows and columns are reversed, which
    can be done by applying a similarity transformation with an exchange matrix $J$,
    where the 1 elements reside on the anti-diagonal and all other elements are 0. In
    particular, the Cholesky decomposition of the transformed precision matrix is
    $J\Lambda J=LL^T$, where $L$ is a lower-triangular matrix. Because $\Lambda=UU^T$
    and $J=J^{-1}=J^T$, the `precisions_cholesky_` for each Gaussian component can be
    expressed as $JLJ$.

    Refer to #26415 for details.

    Parameters
    ----------
    precisions : array-like
        The precision matrix of the current components.
        The shape depends on the covariance_type.

    covariance_type : {'full', 'tied', 'diag', 'spherical'}
        The type of precision matrices.

    Returns
    -------
    precisions_cholesky : array-like
        The cholesky decomposition of sample precisions of the current
        components. The shape depends on the covariance_type.
    """
    if covariance_type == "full":
        precisions_cholesky = xp.stack(
            [
                _flipudlr(
                    _cholesky(_flipudlr(precisions[i, :, :], xp=xp), xp=xp), xp=xp
                )
                for i in range(precisions.shape[0])
            ]
        )
    elif covariance_type == "tied":
        precisions_cholesky = _flipudlr(
            _cholesky(_flipudlr(precisions, xp=xp), xp=xp), xp=xp
        )
    else:
        precisions_cholesky = xp.sqrt(precisions)
    return precisions_cholesky


###############################################################################
# Gaussian mixture probability estimators
def _compute_log_det_cholesky(matrix_chol, covariance_type, n_features, xp=None):
    """Compute the log-det of the cholesky decomposition of matrices.

    Parameters
    ----------
    matrix_chol : array-like
        Cholesky decompositions of the matrices.
        'full' : shape of (n_components, n_features, n_features)
        'tied' : shape of (n_features, n_features)
        'diag' : shape of (n_components, n_features)
        'spherical' : shape of (n_components,)

    covariance_type : {'full', 'tied', 'diag', 'spherical'}

    n_features : int
        Number of features.

    Returns
    -------
    log_det_precision_chol : array-like of shape (n_components,)
        The determinant of the precision matrix for each component.
    """
    xp, _ = get_namespace(matrix_chol, xp=xp)
    if covariance_type == "full":
        n_components, _, _ = matrix_chol.shape
        log_det_chol = xp.sum(
            xp.log(xp.reshape(matrix_chol, (n_components, -1))[:, :: n_features + 1]),
            axis=1,
        )

    elif covariance_type == "tied":
        log_det_chol = xp.sum(xp.log(xp.linalg.diagonal(matrix_chol)))

    elif covariance_type == "diag":
        log_det_chol = xp.sum(xp.log(matrix_chol), axis=1)

    else:
        log_det_chol = n_features * xp.log(matrix_chol)

    return log_det_chol


def _estimate_log_gaussian_prob(X, means, precisions_chol, covariance_type, xp=None):
    """Estimate the log Gaussian probability.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)

    means : array-like of shape (n_components, n_features)

    precisions_chol : array-like
        Cholesky decompositions of the precision matrices.
        'full' : shape of (n_components, n_features, n_features)
        'tied' : shape of (n_features, n_features)
        'diag' : shape of (n_components, n_features)
        'spherical' : shape of (n_components,)

    covariance_type : {'full', 'tied', 'diag', 'spherical'}

    Returns
    -------
    log_prob : array, shape (n_samples, n_components)
    """
    xp, _, device_ = get_namespace_and_device(X, means, precisions_chol, xp=xp)
    n_samples, n_features = X.shape
    n_components, _ = means.shape
    # The determinant of the precision matrix from the Cholesky decomposition
    # corresponds to the negative half of the determinant of the full precision
    # matrix.
    # In short: det(precision_chol) = - det(precision) / 2
    log_det = _compute_log_det_cholesky(precisions_chol, covariance_type, n_features)

    if covariance_type == "full":
        log_prob = xp.empty((n_samples, n_components), dtype=X.dtype, device=device_)
        for k in range(means.shape[0]):
            mu = means[k, :]
            prec_chol = precisions_chol[k, :, :]
            y = (X @ prec_chol) - (mu @ prec_chol)
            log_prob[:, k] = xp.sum(xp.square(y), axis=1)

    elif covariance_type == "tied":
        log_prob = xp.empty((n_samples, n_components), dtype=X.dtype, device=device_)
        for k in range(means.shape[0]):
            mu = means[k, :]
            y = (X @ precisions_chol) - (mu @ precisions_chol)
            log_prob[:, k] = xp.sum(xp.square(y), axis=1)

    elif covariance_type == "diag":
        precisions = precisions_chol**2
        log_prob = (
            xp.sum((means**2 * precisions), axis=1)
            - 2.0 * (X @ (means * precisions).T)
            + (X**2 @ precisions.T)
        )

    elif covariance_type == "spherical":
        precisions = precisions_chol**2
        log_prob = (
            xp.sum(means**2, axis=1) * precisions
            - 2 * (X @ means.T * precisions)
            + xp.linalg.outer(row_norms(X, squared=True), precisions)
        )
    # Since we are using the precision of the Cholesky decomposition,
    # `- 0.5 * log_det_precision` becomes `+ log_det_precision_chol`
    return -0.5 * (n_features * math.log(2 * math.pi) + log_prob) + log_det


class GaussianMixture(BaseMixture):
    """Gaussian Mixture.

    Representation of a Gaussian mixture model probability distribution.
    This class allows to estimate the parameters of a Gaussian mixture
    distribution.

    Read more in the :ref:`User Guide <gmm>`.

    .. versionadded:: 0.18

    Parameters
    ----------
    n_components : int, default=1
        The number of mixture components.

    covariance_type : {'full', 'tied', 'diag', 'spherical'}, default='full'
        String describing the type of covariance parameters to use.
        Must be one of:

        - 'full': each component has its own general covariance matrix.
        - 'tied': all components share the same general covariance matrix.
        - 'diag': each component has its own diagonal covariance matrix.
        - 'spherical': each component has its own single variance.

        For an example of using `covariance_type`, refer to
        :ref:`sphx_glr_auto_examples_mixture_plot_gmm_selection.py`.

    tol : float, default=1e-3
        The convergence threshold. EM iterations will stop when the
        lower bound average gain is below this threshold.

    reg_covar : float, default=1e-6
        Non-negative regularization added to the diagonal of covariance.
        Allows to assure that the covariance matrices are all positive.

    max_iter : int, default=100
        The number of EM iterations to perform.

    n_init : int, default=1
        The number of initializations to perform. The best results are kept.

    init_params : {'kmeans', 'k-means++', 'random', 'random_from_data'}, \
    default='kmeans'
        The method used to initialize the weights, the means and the
        precisions.
        String must be one of:

        - 'kmeans' : responsibilities are initialized using kmeans.
        - 'k-means++' : use the k-means++ method to initialize.
        - 'random' : responsibilities are initialized randomly.
        - 'random_from_data' : initial means are randomly selected data points.

        .. versionchanged:: v1.1
            `init_params` now accepts 'random_from_data' and 'k-means++' as
            initialization methods.

    weights_init : array-like of shape (n_components, ), default=None
        The user-provided initial weights.
        If it is None, weights are initialized using the `init_params` method.

    means_init : array-like of shape (n_components, n_features), default=None
        The user-provided initial means,
        If it is None, means are initialized using the `init_params` method.

    precisions_init : array-like, default=None
        The user-provided initial precisions (inverse of the covariance
        matrices).
        If it is None, precisions are initialized using the 'init_params'
        method.
        The shape depends on 'covariance_type'::

            (n_components,)                        if 'spherical',
            (n_features, n_features)               if 'tied',
            (n_components, n_features)             if 'diag',
            (n_components, n_features, n_features) if 'full'

    random_state : int, RandomState instance or None, default=None
        Controls the random seed given to the method chosen to initialize the
        parameters (see `init_params`).
        In addition, it controls the generation of random samples from the
        fitted distribution (see the method `sample`).
        Pass an int for reproducible output across multiple function calls.
        See :term:`Glossary <random_state>`.

    warm_start : bool, default=False
        If 'warm_start' is True, the solution of the last fitting is used as
        initialization for the next call of fit(). This can speed up
        convergence when fit is called several times on similar problems.
        In that case, 'n_init' is ignored and only a single initialization
        occurs upon the first call.
        See :term:`the Glossary <warm_start>`.

    verbose : int, default=0
        Enable verbose output. If 1 then it prints the current
        initialization and each iteration step. If greater than 1 then
        it prints also the log probability and the time needed
        for each step.

    verbose_interval : int, default=10
        Number of iteration done before the next print.

    Attributes
    ----------
    weights_ : array-like of shape (n_components,)
        The weights of each mixture components.

    means_ : array-like of shape (n_components, n_features)
        The mean of each mixture component.

    covariances_ : array-like
        The covariance of each mixture component.
        The shape depends on `covariance_type`::

            (n_components,)                        if 'spherical',
            (n_features, n_features)               if 'tied',
            (n_components, n_features)             if 'diag',
            (n_components, n_features, n_features) if 'full'

        For an example of using covariances, refer to
        :ref:`sphx_glr_auto_examples_mixture_plot_gmm_covariances.py`.

    precisions_ : array-like
        The precision matrices for each component in the mixture. A precision
        matrix is the inverse of a covariance matrix. A covariance matrix is
        symmetric positive definite so the mixture of Gaussian can be
        equivalently parameterized by the precision matrices. Storing the
        precision matrices instead of the covariance matrices makes it more
        efficient to compute the log-likelihood of new samples at test time.
        The shape depends on `covariance_type`::

            (n_components,)                        if 'spherical',
            (n_features, n_features)               if 'tied',
            (n_components, n_features)             if 'diag',
            (n_components, n_features, n_features) if 'full'

    precisions_cholesky_ : array-like
        The cholesky decomposition of the precision matrices of each mixture
        component. A precision matrix is the inverse of a covariance matrix.
        A covariance matrix is symmetric positive definite so the mixture of
        Gaussian can be equivalently parameterized by the precision matrices.
        Storing the precision matrices instead of the covariance matrices makes
        it more efficient to compute the log-likelihood of new samples at test
        time. The shape depends on `covariance_type`::

            (n_components,)                        if 'spherical',
            (n_features, n_features)               if 'tied',
            (n_components, n_features)             if 'diag',
            (n_components, n_features, n_features) if 'full'

    converged_ : bool
        True when convergence of the best fit of EM was reached, False otherwise.

    n_iter_ : int
        Number of step used by the best fit of EM to reach the convergence.

    lower_bound_ : float
        Lower bound value on the log-likelihood (of the training data with
        respect to the model) of the best fit of EM.

    lower_bounds_ : array-like of shape (`n_iter_`,)
        The list of lower bound values on the log-likelihood from each
        iteration of the best fit of EM.

    n_features_in_ : int
        Number of features seen during :term:`fit`.

        .. versionadded:: 0.24

    feature_names_in_ : ndarray of shape (`n_features_in_`,)
        Names of features seen during :term:`fit`. Defined only when `X`
        has feature names that are all strings.

        .. versionadded:: 1.0

    See Also
    --------
    BayesianGaussianMixture : Gaussian mixture model fit with a variational
        inference.

    Examples
    --------
    >>> import numpy as np
    >>> from sklearn.mixture import GaussianMixture
    >>> X = np.array([[1, 2], [1, 4], [1, 0], [10, 2], [10, 4], [10, 0]])
    >>> gm = GaussianMixture(n_components=2, random_state=0).fit(X)
    >>> gm.means_
    array([[10.,  2.],
           [ 1.,  2.]])
    >>> gm.predict([[0, 0], [12, 3]])
    array([1, 0])

    For a comparison of Gaussian Mixture with other clustering algorithms, see
    :ref:`sphx_glr_auto_examples_cluster_plot_cluster_comparison.py`
    """

    _parameter_constraints: dict = {
        **BaseMixture._parameter_constraints,
        "covariance_type": [StrOptions({"full", "tied", "diag", "spherical"})],
        "weights_init": ["array-like", None],
        "means_init": ["array-like", None],
        "precisions_init": ["array-like", None],
    }

    def __init__(
        self,
        n_components=1,
        *,
        covariance_type="full",
        tol=1e-3,
        reg_covar=1e-6,
        max_iter=100,
        n_init=1,
        init_params="kmeans",
        weights_init=None,
        means_init=None,
        precisions_init=None,
        random_state=None,
        warm_start=False,
        verbose=0,
        verbose_interval=10,
    ):
        super().__init__(
            n_components=n_components,
            tol=tol,
            reg_covar=reg_covar,
            max_iter=max_iter,
            n_init=n_init,
            init_params=init_params,
            random_state=random_state,
            warm_start=warm_start,
            verbose=verbose,
            verbose_interval=verbose_interval,
        )

        self.covariance_type = covariance_type
        self.weights_init = weights_init
        self.means_init = means_init
        self.precisions_init = precisions_init

    def _check_parameters(self, X, xp=None):
        """Check the Gaussian mixture parameters are well defined."""
        _, n_features = X.shape

        if self.weights_init is not None:
            self.weights_init = _check_weights(
                self.weights_init, self.n_components, xp=xp
            )

        if self.means_init is not None:
            self.means_init = _check_means(
                self.means_init, self.n_components, n_features, xp=xp
            )

        if self.precisions_init is not None:
            self.precisions_init = _check_precisions(
                self.precisions_init,
                self.covariance_type,
                self.n_components,
                n_features,
                xp=xp,
            )

        allowed_init_params = ["random", "random_from_data"]
        if (
            get_config()["array_api_dispatch"]
            and self.init_params not in allowed_init_params
        ):
            raise NotImplementedError(
                f"Allowed `init_params` are {allowed_init_params} if "
                f"'array_api_dispatch' is enabled. You passed "
                f"init_params={self.init_params!r}, which are not implemented to work "
                "with 'array_api_dispatch' enabled. Please disable "
                f"'array_api_dispatch' to use init_params={self.init_params!r}."
            )

    def _initialize_parameters(self, X, random_state, xp=None):
        # If all the initial parameters are all provided, then there is no need to run
        # the initialization.
        compute_resp = (
            self.weights_init is None
            or self.means_init is None
            or self.precisions_init is None
        )
        if compute_resp:
            super()._initialize_parameters(X, random_state, xp=xp)
        else:
            self._initialize(X, None, xp=xp)

    def _initialize(self, X, resp, xp=None):
        """Initialization of the Gaussian mixture parameters.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)

        resp : array-like of shape (n_samples, n_components)
        """
        xp, _, device_ = get_namespace_and_device(X, xp=xp)
        n_samples, _ = X.shape
        weights, means, covariances = None, None, None
        if resp is not None:
            weights, means, covariances = _estimate_gaussian_parameters(
                X, resp, self.reg_covar, self.covariance_type, xp=xp
            )
            if self.weights_init is None:
                weights /= n_samples

        self.weights_ = weights if self.weights_init is None else self.weights_init
        self.weights_ = xp.asarray(self.weights_, device=device_)

        self.means_ = means if self.means_init is None else self.means_init

        if self.precisions_init is None:
            self.covariances_ = covariances
            self.precisions_cholesky_ = _compute_precision_cholesky(
                covariances, self.covariance_type, xp=xp
            )
        else:
            self.precisions_cholesky_ = _compute_precision_cholesky_from_precisions(
                self.precisions_init, self.covariance_type, xp=xp
            )

    def _m_step(self, X, log_resp, xp=None):
        """M step.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)

        log_resp : array-like of shape (n_samples, n_components)
            Logarithm of the posterior probabilities (or responsibilities) of
            the point of each sample in X.
        """
        xp, _ = get_namespace(X, log_resp, xp=xp)
        self.weights_, self.means_, self.covariances_ = _estimate_gaussian_parameters(
            X, xp.exp(log_resp), self.reg_covar, self.covariance_type, xp=xp
        )
        self.weights_ /= xp.sum(self.weights_)
        self.precisions_cholesky_ = _compute_precision_cholesky(
            self.covariances_, self.covariance_type, xp=xp
        )

    def _estimate_log_prob(self, X, xp=None):
        return _estimate_log_gaussian_prob(
            X, self.means_, self.precisions_cholesky_, self.covariance_type, xp=xp
        )

    def _estimate_log_weights(self, xp=None):
        xp, _ = get_namespace(self.weights_, xp=xp)
        return xp.log(self.weights_)

    def _compute_lower_bound(self, _, log_prob_norm):
        return log_prob_norm

    def _get_parameters(self):
        return (
            self.weights_,
            self.means_,
            self.covariances_,
            self.precisions_cholesky_,
        )

    def _set_parameters(self, params, xp=None):
        xp, _, device_ = get_namespace_and_device(params, xp=xp)
        (
            self.weights_,
            self.means_,
            self.covariances_,
            self.precisions_cholesky_,
        ) = params

        # Attributes computation
        if self.covariance_type == "full":
            self.precisions_ = xp.empty_like(self.precisions_cholesky_, device=device_)
            for k in range(self.precisions_cholesky_.shape[0]):
                prec_chol = self.precisions_cholesky_[k, :, :]
                self.precisions_[k, :, :] = prec_chol @ prec_chol.T

        elif self.covariance_type == "tied":
            self.precisions_ = self.precisions_cholesky_ @ self.precisions_cholesky_.T

        else:
            self.precisions_ = self.precisions_cholesky_**2

    def _n_parameters(self):
        """Return the number of free parameters in the model."""
        _, n_features = self.means_.shape
        if self.covariance_type == "full":
            cov_params = self.n_components * n_features * (n_features + 1) / 2.0
        elif self.covariance_type == "diag":
            cov_params = self.n_components * n_features
        elif self.covariance_type == "tied":
            cov_params = n_features * (n_features + 1) / 2.0
        elif self.covariance_type == "spherical":
            cov_params = self.n_components
        mean_params = n_features * self.n_components
        return int(cov_params + mean_params + self.n_components - 1)

    def bic(self, X):
        """Bayesian information criterion for the current model on the input X.

        You can refer to this :ref:`mathematical section <aic_bic>` for more
        details regarding the formulation of the BIC used.

        For an example of GMM selection using `bic` information criterion,
        refer to :ref:`sphx_glr_auto_examples_mixture_plot_gmm_selection.py`.

        Parameters
        ----------
        X : array of shape (n_samples, n_dimensions)
            The input samples.

        Returns
        -------
        bic : float
            The lower the better.
        """
        return -2 * self.score(X) * X.shape[0] + self._n_parameters() * math.log(
            X.shape[0]
        )

    def aic(self, X):
        """Akaike information criterion for the current model on the input X.

        You can refer to this :ref:`mathematical section <aic_bic>` for more
        details regarding the formulation of the AIC used.

        Parameters
        ----------
        X : array of shape (n_samples, n_dimensions)
            The input samples.

        Returns
        -------
        aic : float
            The lower the better.
        """
        return -2 * self.score(X) * X.shape[0] + 2 * self._n_parameters()

    def __sklearn_tags__(self):
        tags = super().__sklearn_tags__()
        tags.array_api_support = (
            self.init_params in ["random", "random_from_data"] and not self.warm_start
        )
        return tags
