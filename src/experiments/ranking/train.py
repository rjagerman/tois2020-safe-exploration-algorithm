import logging
import numpy as np
import numba
import matplotlib
import json
from matplotlib import pyplot as plt
from scipy import stats as st
from joblib.memory import Memory
from argparse import ArgumentParser
from rulpy.pipeline import task, TaskExecutor
from experiments.util import rng_seed, get_evaluation_points, mkdir_if_not_exists, NumpyEncoder
from experiments.ranking.dataset import load_test, load_train
from experiments.ranking.policies import create_policy
from experiments.ranking.evaluation import evaluate
from experiments.ranking.optimization import optimize
from experiments.ranking.baseline import best_baseline
from ltrpy.clicks.position import position_binarized_5, near_random_5
from ltrpy.clicks.cascading import perfect_5
from ltrpy.clicks.cascading import navigational_5
from ltrpy.clicks.cascading import informational_5


def main():
    logging.basicConfig(format="[%(asctime)s] %(levelname)s %(threadName)s: %(message)s",
                        level=logging.INFO)
    cli_parser = ArgumentParser()
    cli_parser.add_argument("-c", "--config", type=str, required=True)
    cli_parser.add_argument("-d", "--dataset", type=str, required=True)
    cli_parser.add_argument("-b", "--behavior", choices=('position', 'perfect', 'navigational', 'informational', 'nearrandom'), default='position')
    cli_parser.add_argument("-r", "--repeats", type=int, default=15)
    cli_parser.add_argument("-p", "--parallel", type=int, default=1)
    cli_parser.add_argument("-o", "--output", type=str, required=True)
    cli_parser.add_argument("--cache", type=str, default="cache")
    cli_parser.add_argument("--iterations", type=int, default=1000000)
    cli_parser.add_argument("--evaluations", type=int, default=50)
    cli_parser.add_argument("--eval_scale", choices=('lin', 'log'), default='log')
    args = cli_parser.parse_args()

    parser = ArgumentParser()
    parser.add_argument("--strategy", type=str, default='online')
    parser.add_argument("--lr", type=float, default=0.01)
    parser.add_argument("--cap", type=float, default=0.01)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--label", type=str, default=None)

    # Read experiment configuration
    with open(args.config, 'rt') as f:
        lines = f.readlines()
        configs = [parser.parse_args(line.strip().split(" ")) for line in lines]

    # Run experiments in task executor
    with TaskExecutor(max_workers=args.parallel, memory=Memory(args.cache, compress=6)):
        results = [run_experiment(config, args.dataset, args.behavior, args.repeats, args.iterations, args.evaluations, args.eval_scale) for config in configs]
    results = [r.result for r in results]

    # Write json results
    mkdir_if_not_exists(f"results/{args.output}.json")
    with open(f"results/{args.output}.json", "wt") as f:
        js_results = [{"result": result, "args": vars(config)} for result, config in zip(results, configs)]
        json.dump(js_results, f, cls=NumpyEncoder)

    # Print results
    for result, config in sorted(zip(results, configs), key=lambda e: e[0]['conf'][0][-1], reverse=True):
        logging.info(f"{args.dataset} {args.behavior} {config.strategy} ({config.lr}): {result['mean'][-1]} +/- {result['std'][-1]} => {result['conf'][0][-1]}")

    # Create plot
    fig, ax = plt.subplots()
    for config, result in zip(configs, results):
        label = f"{config.strategy} ({config.lr})" if config.label is None else config.label
        x = result['x']
        y = result['mean']
        y_std = result['std']
        ax.plot(x, y, label=label)
        ax.fill_between(x, y - y_std, y + y_std, alpha=0.35)
    if args.eval_scale == 'log':
        ax.set_xscale('symlog')
    ax.set_xlabel('Time $t$')
    ax.set_ylabel('ndcg@10')
    ax.legend()
    mkdir_if_not_exists(f"plots/{args.output}.pdf")
    fig.savefig(f"plots/{args.output}.pdf")



@task(use_cache=True)
async def run_experiment(config, data, behavior, repeats, iterations, evaluations, eval_scale, seed_base=4200):

    # points to evaluate at
    points = get_evaluation_points(iterations, evaluations, eval_scale)
    
    # Evaluate at all points and all seeds
    results = []
    for seed in range(seed_base, seed_base + repeats):
        results.append(ranking_run(config, data, behavior, points, seed))

    # Await results to finish computing
    results = np.vstack([await r for r in results])

    # Compute aggregate statistics from results
    out = {
        "mean": np.mean(results, axis=0),
        "std": np.std(results, axis=0),
        "conf": st.t.interval(0.95, results.shape[0] - 1, loc=np.mean(results, axis=0), scale=st.sem(results, axis=0)),
        "n": results.shape[0],
        "x": points
    }
    
    # Return results
    return out


@task(use_cache=True)
async def ranking_run(config, data, behavior, points, seed):

    # Load train, test and policy
    train = load_train(data, seed)
    test = load_test(data, seed)
    baseline = best_baseline(data, seed)
    train, test, baseline = await train, await test, await baseline

    # Data structure to hold output results
    out = np.zeros(len(points))

    # Seed randomness
    prng = rng_seed(seed)

    # Build policy
    args = {'d': train.d, 'baseline': baseline}
    args.update(vars(config))
    if behavior in ['perfect']:
        args['eta'] = 0.0
    else:
        args['eta'] = 1.0
    policy = create_policy(**args)

    # Build behavior model
    click_model = build_click_model(behavior)

    # Generate training indices and seed randomness
    indices = prng.randint(0, train.size, np.max(points))

    # Evaluate on point 0
    out[0] = evaluate(test, policy)
    logging.info(f"[{seed}, {0:7d}] {data} {behavior} {config.strategy} ({config.lr}): {out[0]:.5f}")

    # Train and evaluate at specified points
    for i in range(1, len(points)):
        start = points[i - 1]
        end = points[i]
        optimize(train, indices[start:end], policy, click_model)
        out[i] = evaluate(test, policy)
        logging.info(f"[{seed}, {end:7d}] {data} {behavior} {config.strategy} ({config.lr}): {out[i]:.5f}")

    return out


def build_click_model(behavior):
    return {
        'position': position_binarized_5,
        'perfect': perfect_5,
        'navigational': navigational_5,
        'informational': informational_5,
        'nearrandom': near_random_5
    }[behavior](10)


if __name__ == "__main__":
    main()
