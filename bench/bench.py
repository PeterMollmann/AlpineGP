# import matplotlib.pyplot as plt
from deap import gp

from alpine.gp import gpsymbreg as gps
from alpine.data import Dataset
from alpine.gp import util
import numpy as np
import ray
import yaml

import time

import warnings
import pygmo as pg

import re
from sklearn.metrics import r2_score
from datasets import generate_dataset

import mygrad as mg
from mygrad._utils.lock_management import mem_guard_off


num_cpus = 1
num_runs = 1  # 20


def check_trig_fn(ind):
    return len(re.findall("cos", str(ind))) + len(re.findall("sin", str(ind)))


def check_nested_trig_fn(ind):
    return util.detect_nested_trigonometric_functions(str(ind))


def eval_model(individual, D, consts=[]):
    warnings.filterwarnings("ignore")
    y_pred = individual(*D.X, consts)
    return y_pred


def compute_MSE(individual, D, consts=[]):
    y_pred = eval_model(individual, D, consts)
    MSE = np.mean((D.y - y_pred) ** 2)

    if np.isnan(MSE) or np.isinf(MSE):
        MSE = 1e8

    return MSE


# TODO: this could become a library function


def compile_individual_with_consts(tree, toolbox, special_term_name="a"):
    const_idx = 0
    tree_clone = toolbox.clone(tree)
    for i, node in enumerate(tree_clone):
        if isinstance(node, gp.Terminal) and node.name[0:3] != "ARG":
            if node.name == special_term_name:
                new_node_name = special_term_name + "[" + str(const_idx) + "]"
                tree_clone[i] = gp.Terminal(new_node_name, True, float)
                const_idx += 1

    individual = toolbox.compile(expr=tree_clone, extra_args=[special_term_name])
    return individual, const_idx


def eval_MSE_and_tune_constants(tree, toolbox, D):
    individual, num_consts = compile_individual_with_consts(tree, toolbox)

    if num_consts > 0:

        # TODO: do we really need to redefine this function instead of using the one
        # outside?
        def eval_MSE(consts):
            warnings.filterwarnings("ignore")
            y_pred = individual(*D.X, consts)
            total_err = np.mean((D.y - y_pred) ** 2)

            return total_err

        x0 = np.ones(num_consts)

        class fitting_problem:
            def fitness(self, x):
                total_err = eval_MSE(x)
                # return [total_err + 0.*(np.linalg.norm(x, 2))**2]
                return [total_err]

            def gradient(self, x):
                with mem_guard_off:
                    xt = mg.tensor(x, copy=False)
                    f = self.fitness(xt)[0]
                    f.backward()
                return xt.grad

            def get_bounds(self):
                return (-5.0 * np.ones(num_consts), 5.0 * np.ones(num_consts))

        # PYGMO SOLVER
        prb = pg.problem(fitting_problem())
        algo = pg.algorithm(pg.nlopt(solver="lbfgs"))
        # algo = pg.algorithm(pg.pso(gen=10))
        # pop = pg.population(prb, size=70)
        algo.extract(pg.nlopt).maxeval = 10
        pop = pg.population(prb, size=1)
        pop.push_back(x0)
        pop = algo.evolve(pop)
        MSE = pop.champion_f[0]
        consts = pop.champion_x

        if np.isinf(MSE) or np.isnan(MSE):
            MSE = 1e8
    else:
        MSE = compute_MSE(individual, D)
        consts = []
    return MSE, consts


def get_features_batch(
    individuals_str_batch,
    individ_feature_extractors=[len, check_nested_trig_fn, check_trig_fn],
):
    features_batch = [
        [fe(i) for i in individuals_str_batch] for fe in individ_feature_extractors
    ]

    individ_length = features_batch[0]
    nested_trigs = features_batch[1]
    num_trigs = features_batch[2]
    return individ_length, nested_trigs, num_trigs


@ray.remote(num_cpus=num_cpus)
def predict(individuals_str_batch, toolbox, dataset, penalty, fitness_scale):

    predictions = [None] * len(individuals_str_batch)

    for i, tree in enumerate(individuals_str_batch):
        callable, _ = compile_individual_with_consts(tree, toolbox)
        predictions[i] = eval_model(callable, dataset, consts=tree.consts)

    return predictions


@ray.remote(num_cpus=num_cpus)
def compute_MSEs(individuals_str_batch, toolbox, dataset, penalty, fitness_scale):

    total_errs = [None] * len(individuals_str_batch)

    for i, tree in enumerate(individuals_str_batch):
        callable, _ = compile_individual_with_consts(tree, toolbox)
        total_errs[i] = compute_MSE(callable, dataset, consts=tree.consts)

    return total_errs


@ray.remote(num_cpus=num_cpus)
def compute_attributes(individuals_str_batch, toolbox, dataset, penalty, fitness_scale):

    attributes = [None] * len(individuals_str_batch)

    individ_length, nested_trigs, num_trigs = get_features_batch(individuals_str_batch)

    for i, tree in enumerate(individuals_str_batch):

        # Tarpeian selection
        if individ_length[i] >= 50:
            consts = None
            fitness = (1e8,)
        else:
            MSE, consts = eval_MSE_and_tune_constants(tree, toolbox, dataset)
            fitness = (
                fitness_scale
                * (
                    MSE
                    + 100000 * nested_trigs[i]
                    + 0.0 * num_trigs[i]
                    + penalty["reg_param"] * individ_length[i]
                ),
            )
        attributes[i] = {"consts": consts, "fitness": fitness}
    return attributes


def assign_attributes(individuals, attributes):
    for ind, attr in zip(individuals, attributes):
        ind.consts = attr["consts"]
        ind.fitness.values = attr["fitness"]


def eval(problem, cfgfile, seed=42):

    with open(cfgfile) as config_file:
        config_file_data = yaml.safe_load(config_file)

    scaleXy = config_file_data["gp"]["scaleXy"]

    # generate training and test datasets
    (
        X_train_scaled,
        y_train_scaled,
        X_test_scaled,
        y_test,
        _,
        scaler_y,
        num_variables,
        _,
    ) = generate_dataset(problem, scaleXy=scaleXy, random_state=seed)

    if num_variables == 1:
        pset = gp.PrimitiveSetTyped("Main", [float], float)
        pset.renameArguments(ARG0="x")
    elif num_variables == 2:
        pset = gp.PrimitiveSetTyped("Main", [float, float], float)
        pset.renameArguments(ARG0="x")
        pset.renameArguments(ARG1="y")
    else:
        pset = gp.PrimitiveSetTyped("Main", [float] * num_variables, float)

    batch_size = config_file_data["gp"]["batch_size"]
    if config_file_data["gp"]["use_constants"]:
        pset.addTerminal(object, float, "a")

    callback_func = assign_attributes
    fitness_scale = 1.0

    # if problem == "C1":
    #     batch_size = 100
    #     fitness_scale = 1e6
    #     config_file_data["gp"]["penalty"]["reg_param"] = 0.0
    #     pset.addTerminal(object, float, "a")

    penalty = config_file_data["gp"]["penalty"]
    common_params = {"penalty": penalty, "fitness_scale": fitness_scale}

    gpsr = gps.GPSymbolicRegressor(
        pset=pset,
        fitness=compute_attributes.remote,
        predict_func=predict.remote,
        error_metric=compute_MSEs.remote,
        common_data=common_params,
        callback_func=callback_func,
        print_log=False,
        num_best_inds_str=1,
        config_file_data=config_file_data,
        save_best_individual=False,
        output_path="./",
        seed=None,
        batch_size=batch_size,
    )

    train_data = Dataset("dataset", X_train_scaled, y_train_scaled)
    test_data = Dataset("dataset", X_test_scaled, y_test)

    if num_variables > 1:
        train_data.X = [train_data.X[:, i] for i in range(num_variables)]
        test_data.X = [test_data.X[:, i] for i in range(num_variables)]
    else:
        train_data.X = [train_data.X]
        test_data.X = [test_data.X]

    tic = time.time()
    gpsr.fit(train_data)
    toc = time.time()

    if hasattr(gpsr.best, "consts"):
        print("Best parameters = ", gpsr.best.consts)

    print("Elapsed time = ", toc - tic)
    individuals_per_sec = (
        (gpsr.cgen + 1) * gpsr.NINDIVIDUALS * gpsr.num_islands / (toc - tic)
    )
    print("Individuals per sec = ", individuals_per_sec)

    u_best = gpsr.predict(test_data)
    # print(u_best)
    # print(y_test)

    # plt.figure()
    # plt.plot(u_best)
    # plt.plot(y_test, "+")
    # plt.show()

    # de-scale outputs before computing errors
    if scaleXy:
        u_best = scaler_y.inverse_transform(u_best.reshape(-1, 1)).flatten()

    MSE = np.mean((u_best - y_test) ** 2)
    r2_test = r2_score(y_test, u_best)
    print("MSE on the test set = ", MSE)
    print("R^2 on the test set = ", r2_test)

    pred_train = gpsr.predict(train_data)

    if scaleXy:
        pred_train = scaler_y.inverse_transform(pred_train.reshape(-1, 1)).flatten()
        y_train_scaled = scaler_y.inverse_transform(
            y_train_scaled.reshape(-1, 1)
        ).flatten()

    MSE = np.mean((pred_train - y_train_scaled) ** 2)
    r2_train = r2_score(y_train_scaled, pred_train)
    print("MSE on the training set = ", MSE)
    print("R^2 on the training set = ", r2_train)
    # Nguyen
    # if MSE <= 1e-10 or (problem == "Nguyen-13" and MSE <= 1e-5):
    #     return 1.0
    # else:
    #     return 0.0

    return r2_train, r2_test


if __name__ == "__main__":
    import argparse
    import pathlib
    import ray

    # problems = [
    #     "Nguyen-1",
    #     "Nguyen-2",
    #     "Nguyen-3",
    #     "Nguyen-4",
    #     "Nguyen-5",
    #     "Nguyen-6",
    #     "Nguyen-7",
    #     "Nguyen-8",
    #     "Nguyen-9",
    #     "Nguyen-10",
    #     "Nguyen-11",
    #     "Nguyen-12",
    #     "Nguyen-13",
    # ]

    parser = argparse.ArgumentParser()

    parser.add_argument("problem", help="Name of the PMLB or Nguyen dataset.")
    parser.add_argument(
        "-c",
        type=pathlib.Path,
        metavar="cfgfile",
        help="Path of the YAML config file for the problem.",
    )

    args = parser.parse_args()

    problem = args.problem
    cfgfile = args.c

    # problem = "1089_USCrime"

    seeds = [29802, 22118, 860, 15795, 21575, 5390, 11964, 6265, 23654, 11284]

    r2_tests = []

    # possibly add rmse_train, rmse_test, rmse_val
    header = ["problem", "trial", "r2_train", "r2_test", "seed"]

    with open(f"./results/{problem}.csv", "w") as f:
        for h in header:
            f.write(h)
            f.write("\n" if h == header[-1] else ";")

    for i, seed in enumerate(seeds):
        print("PROBLEM: ", problem)
        r2_train, r2_test = eval(problem=problem, cfgfile=cfgfile, seed=seed)
        r2_tests.append(r2_test)

        stats = {
            "problem": problem,
            "trial": i + 1,
            "r2_train": r2_train,
            "r2_test": r2_test,
            "seed": seed,
        }

        with open(f"./results/{problem}.csv", "a") as f:
            for h in header:
                f.write(f"{stats[h]}")
                f.write("\n" if h == header[-1] else ";")

    print("Median Test R^2 = ", np.median(r2_tests))

    ray.shutdown()
    # Nguyen
    # ave_success_rate = 0.0
    # with open("bench_stats.txt", "w") as file:
    #     for problem in problems:
    #         success = 0.0
    #         for i in range(num_runs):
    #             print("Problem {prb}, RUN #{num}".format(prb=problem, num=i))
    #             success += bench(problem=problem)
    #         success_rate = success / num_runs * 100
    #         ave_success_rate += success_rate / len(problems)
    #         str_to_print = problem + " " + str(success_rate)
    #         print(str_to_print, file=file, flush=True)
    #     print("Average success rate = ", ave_success_rate)
