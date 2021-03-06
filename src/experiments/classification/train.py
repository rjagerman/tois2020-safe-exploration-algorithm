import logging
import numpy as np
import numba
import matplotlib
import json
from matplotlib import pyplot as plt
from scipy import stats as st
from joblib.memory import Memory
from argparse import ArgumentParser
from backflow import task
from backflow.schedulers import MultiThreadScheduler
from backflow.results import sqlite_result
from experiments.classification.policies import create_policy
from experiments.classification.optimization import optimize
from experiments.classification.evaluation import evaluate
from experiments.classification.baseline import best_baseline, statistical_baseline
from experiments.classification.dataset import load_train, load_test, load_vali
from experiments.util import rng_seed, get_evaluation_points, mkdir_if_not_exists, NumpyEncoder


def main():
    logging.basicConfig(format="[%(asctime)s] %(levelname)-5s %(threadName)35s: %(message)s",
                        level=logging.INFO)
    logging.getLogger('sqlitedict').setLevel(logging.WARNING)
    cli_parser = ArgumentParser()
    cli_parser.add_argument("-c", "--config", type=str, required=True)
    cli_parser.add_argument("-d", "--dataset", type=str, required=True)
    cli_parser.add_argument("-r", "--repeats", type=int, default=15)
    cli_parser.add_argument("-p", "--parallel", type=int, default=1)
    cli_parser.add_argument("-o", "--output", type=str, required=True)
    cli_parser.add_argument("--cache", type=str, default="cache")
    cli_parser.add_argument("--iterations", type=int, default=1000000)
    cli_parser.add_argument("--evaluations", type=int, default=50)
    cli_parser.add_argument("--eval_scale", choices=('lin', 'log'), default='log')
    args = cli_parser.parse_args()

    parser = ArgumentParser()
    parser.add_argument("--strategy", type=str, default='epsgreedy')
    parser.add_argument("--cold", action='store_true')
    parser.add_argument("--lr", type=float, default=0.01)
    parser.add_argument("--l2", type=float, default=1.0)
    parser.add_argument("--eps", type=float, default=0.1)
    parser.add_argument("--tau", type=float, default=1.0)
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--cap", type=float, default=0.1)
    parser.add_argument("--label", type=str, default=None)

    # Read experiment configuration
    with open(args.config, 'rt') as f:
        lines = f.readlines()
        configs = [parser.parse_args(line.strip().split(" ")) for line in lines]

    # Run experiments in task executor
    with MultiThreadScheduler(args.parallel) as scheduler:
        results = [run_experiment(config, args.dataset, args.repeats, args.iterations, args.evaluations, args.eval_scale) for config in configs]
        scheduler.block_until_tasks_finish()
    results = [r.result.value for r in results]

    # Write json results
    mkdir_if_not_exists(f"results/{args.output}.json")
    with open(f"results/{args.output}.json", "wt") as f:
        js_results = [{"result": result, "args": vars(config)} for result, config in zip(results, configs)]
        json.dump(js_results, f, cls=NumpyEncoder)

    # Print results
    for metric in ['learned', 'deploy', 'regret']:
        bound = 1 if metric == 'regret' else 0
        reverse = False if metric == 'regret' else True
        for result, config in sorted(zip(results, configs), key=lambda e: e[0][metric]['conf'][bound][-1], reverse=reverse):
            tune_p = config.lr
            if config.strategy in ["ucb", "thompson"]:
                tune_p = config.alpha
            logging.info(f"{args.dataset} {config.strategy} ({tune_p}, {config.l2}) = {metric}: {result[metric]['mean'][-1]:.4f} +/- {result[metric]['std'][-1]:.4f} => {result[metric]['conf'][bound][-1]:.4f}")

    # Create plot
    fig, ax = plt.subplots()
    for config, result in zip(configs, results):
        label = f"{config.strategy} ({config.lr})" if config.label is None else config.label
        x = result['x']
        y = result['deploy']['mean']
        y_std = result['deploy']['std']
        ax.plot(x, y, label=label)
        ax.fill_between(x, y - y_std, y + y_std, alpha=0.35)
    if args.eval_scale == 'log':
        ax.set_xscale('symlog')
        locmin = matplotlib.ticker.SymmetricalLogLocator(base=10.0, subs=np.linspace(1.0, 10.0, 10), linthresh=1.0)
        ax.xaxis.set_minor_locator(locmin)
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        scalar_formatter = matplotlib.ticker.ScalarFormatter()
        log_formatter = matplotlib.ticker.LogFormatterSciNotation(linthresh=1.0)
        def smart_formatter(x, p):
            if x in [1.0, 0.0]:
                return scalar_formatter.format_data(x)
            else:
                return log_formatter.format_data(x)
        func_formatter = matplotlib.ticker.FuncFormatter(smart_formatter)
        ax.xaxis.set_major_formatter(func_formatter)
    ax.set_xlabel('Time $t$')
    ax.set_ylabel('Reward $r \in [0, 1]$')
    #ax.set_ylim([0.0, 1.0])
    ax.legend()
    mkdir_if_not_exists(f"plots/{args.output}.pdf")
    fig.savefig(f"plots/{args.output}.pdf")


@task
async def run_experiment(config, data, repeats, iterations, evaluations, eval_scale, seed_base=4200, vali=0.0):

    # points to evaluate at
    points = get_evaluation_points(iterations, evaluations, eval_scale)

    # Evaluate at all points and all seeds
    results = []
    for seed in range(seed_base, seed_base + repeats):
        results.append(classification_run(config, data, points, seed, vali))

    # Await results to finish computing
    results = [await r for r in results]

    # Combine results with different seeded repeats
    results = {
        "learned": np.vstack([x["learned"] for x in results]),
        "deploy": np.vstack([x["deploy"] for x in results]),
        "regret": np.vstack([x["regret"] for x in results]),
        "test_regret": np.vstack([x["test_regret"] for x in results])
    }

    # Compute aggregate statistics from results
    out = {
        k: {
            "mean": np.mean(results[k], axis=0),
            "std": np.std(results[k], axis=0),
            "conf": st.t.interval(0.95, results[k].shape[0] - 1, loc=np.mean(results[k], axis=0), scale=st.sem(results[k], axis=0)),
            "n": results[k].shape[0]
        }
        for k in results.keys()
    }
    out["x"] = points

    # Return results
    return out


@task(result_fn=sqlite_result(".cache/results.sqlite"))
async def classification_run(config, data, points, seed, vali=0.0):

    # Load train, test and policy
    train = load_train(data, seed)
    test = load_test(data, seed)
    policy = build_policy(config, data, points, seed)
    train, test, policy = await train, await test, await policy
    policy = policy.__deepcopy__()

    # Data structure to hold output results
    out = {
        'deploy': np.zeros(len(points)),
        'learned': np.zeros(len(points)),
        'regret': np.zeros(len(points)),
        'test_regret': np.zeros(len(points)),
    }

    # Generate training indices and seed randomness
    prng = rng_seed(seed)
    indices_shuffle = prng.permutation(train.n)

    train_indices = prng.randint(0, int((1.0 - vali)*train.n), np.max(points))
    train_indices = indices_shuffle[train_indices]

    if vali != 0.0:
        vali_indices = prng.randint(int((1.0 - vali)*train.n), train.n, np.max(points))
        vali_indices = indices_shuffle[vali_indices]
    else:
        vali_indices = train_indices

    # Evaluate on point 0
    if vali == 0.0:
        out['deploy'][0], out['learned'][0] = evaluate(test, policy, np.arange(0, test.n))
    else:
        out['deploy'][0], out['learned'][0] = evaluate(train, policy, indices_shuffle[np.arange(int(vali * train.n), train.n)])
    out['regret'][0] = 0.0
    out['test_regret'][0] = 0.0
    log_progress(0, points, data, out, policy, config, seed)

    # Train and evaluate at specified points
    for i in range(1, len(points)):
        start = points[i - 1]
        end = points[i]
        train_regret, test_regret = optimize(train, np.copy(train_indices[start:end]), np.copy(vali_indices[start:end]), policy)
        out['regret'][i] = out['regret'][i - 1] + train_regret
        out['test_regret'][i] = out['test_regret'][i - 1] + test_regret
        if vali == 0.0:
            out['deploy'][i], out['learned'][i] = evaluate(test, policy, np.arange(0, test.n))
        else:
            out['deploy'][i], out['learned'][i] = evaluate(train, policy, indices_shuffle[np.arange(int(vali * train.n), train.n)])
        log_progress(i, points, data, out, policy, config, seed)

    return out


@task
async def build_policy(config, data, points, seed):
    train = load_train(data, seed)
    if config.strategy in ['ucb', 'thompson']:
        baseline = statistical_baseline(data, config.l2, seed, config.strategy)
    else:
        baseline = best_baseline(data, seed)
    train, baseline = await train, await baseline
    if not config.cold and config.strategy in ['ucb', 'thompson']:
        out = baseline.__deepcopy__()
        out.alpha = config.alpha
        return out
    args = {'k': train.k, 'd': train.d, 'n': train.n, 'baseline': baseline}
    args.update(vars(config))
    if config.strategy in ['sea', 'comp']:
        args['recompute_bounds'] = np.copy(points)
    if not config.cold:
        args['w'] = np.copy(baseline.w)
    # if not config.cold and config.strategy == 'boltzmann' and args['tau'] == 1.0:
    #     args['tau'] = baseline.tau
    return create_policy(**args)


def log_progress(index, points, data, out, policy, config, seed):
    bounds = ""
    if hasattr(policy, 'ucb_baseline') and hasattr(policy, 'lcb_w'):
        bounds = f" :: {policy.lcb_w:.6f} ?> {policy.ucb_baseline:.6f}"
    tune = f"a={config.alpha:.4g}, l2={config.l2:.4g}" if config.strategy in ["ucb", "thompson"] else f"lr={config.lr:.4g}, l2={config.l2:.4g}"
    logging.info(f"[{seed}, {points[index]:7d}] {data} {config.strategy} ({tune}): test deploy:  {out['deploy'][index]:.4f} {bounds}")
    logging.info(f"[{seed}, {points[index]:7d}] {data} {config.strategy} ({tune}): test learned: {out['learned'][index]:.4f}")
    logging.info(f"[{seed}, {points[index]:7d}] {data} {config.strategy} ({tune}): regret:       {out['regret'][index]:.4f}")
    logging.info(f"[{seed}, {points[index]:7d}] {data} {config.strategy} ({tune}): test regret:  {out['test_regret'][index]:.4f}")


if __name__ == "__main__":
    main()
