"""
code that train pursuit with ppo
tested with myPursuit and myPursuit_message for small parameters
code is copied from test_ppo.py in https://github.com/thu-ml/tianshou/blob/master/test/discrete/test_ppo.py

not yet test for reproducibility :(
not yet test on machine :(
"""
import argparse
import os
import pprint

import gymnasium as gym
import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter

from tianshou.data import Collector, PrioritizedVectorReplayBuffer, VectorReplayBuffer
from tianshou.env import DummyVectorEnv, MultiDiscreteToDiscrete, SubprocVectorEnv
from tianshou.trainer import onpolicy_trainer
# from tianshou.utils import TensorboardLogger
from tianshou.utils import WandbLogger
from tianshou.utils.net.common import ActorCritic, DataParallelNet, Net
from tianshou.utils.net.discrete import Actor, Critic
from tianshou.policy import PPOPolicy

import sys
import datetime


# from pursuit_msg.pursuit import my_parallel_env as my_env
from pursuit_msg.pursuit import my_parallel_env_message as my_env
from pursuit_msg.policy.myppo import myPPOPolicy

# sys.path.append("..")
# sys.path.append("../lib")
# sys.path.append("../lib/policy_lib")
# from lib.myppo import myPPOPolicy
# # from lib.myPursuit_gym import my_parallel_env as my_env
# from lib.myPursuit_gym_message import my_parallel_env_message as my_env


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=str, default='pursuit_v4')
    parser.add_argument('--reward-threshold', type=float, default=None)
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--buffer-size', type=int, default=20000)
    parser.add_argument('--lr', type=float, default=3e-4)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--epoch', type=int, default=10)
    parser.add_argument('--step-per-epoch', type=int, default=50000)
    parser.add_argument('--step-per-collect', type=int, default=2000)
    parser.add_argument('--repeat-per-collect', type=int, default=10)
    parser.add_argument('--batch-size', type=int, default=64)
    parser.add_argument('--hidden-sizes', type=int, nargs='*', default=[64, 64])
    parser.add_argument('--training-num', type=int, default=20)
    parser.add_argument('--test-num', type=int, default=100)
    parser.add_argument('--logdir', type=str, default='log')
    parser.add_argument('--render', type=float, default=0.)
    parser.add_argument(
        '--device', type=str, default='cuda' if torch.cuda.is_available() else 'cpu'
    )
    # ppo special
    parser.add_argument('--vf-coef', type=float, default=0.5)
    parser.add_argument('--ent-coef', type=float, default=0.0)
    parser.add_argument('--eps-clip', type=float, default=0.2)
    parser.add_argument('--max-grad-norm', type=float, default=0.5)
    parser.add_argument('--gae-lambda', type=float, default=0.95)
    parser.add_argument('--rew-norm', type=int, default=0)
    parser.add_argument('--norm-adv', type=int, default=0)
    parser.add_argument('--recompute-adv', type=int, default=0)
    parser.add_argument('--dual-clip', type=float, default=None)
    parser.add_argument('--value-clip', type=int, default=0)
    args = parser.parse_known_args()[0]
    return args


def test_ppo(args=get_args()):
    task_parameter = {
        "shared_reward": False,
        "surround": False,
        "freeze_evaders": True,

        "x_size": 10,
        "y_size": 10,
        "obs_range": 5,
        "max_cycles": 40,

        "n_evaders": 2,
        "n_pursuers": 5,

        "catch_reward": 0.5,
        "urgency_reward": -0.05,
        "n_catch": 1,
        "tag_reward": 0,
    }
    args.epoch = 100
    args.hidden_sizes = [512, 512]
    args.lr = 3e-5
    if args.seed is None:
        args.seed = int(np.random.rand() * 100000)

    train_very_fast = False
    if train_very_fast:
        # Set the following parameters so that the program run very fast but train nothing
        task_parameter["max_cycles"] = 50  # 500
        task_parameter["x_size"] = 8  # 16
        task_parameter["y_size"] = 8  # 16
        task_parameter["obs_range"] = 5  # 7, should be odd
        args.training_num = 5  # 10
        args.test_num = 5  # 100
        args.hidden_sizes = [64, 64]  # [128, 128, 128, 128]
        args.epoch = 2  # 10  # 20
        args.step_per_epoch = 10  # 500  # 10000
        args.render = 0.05
        args.logdir = "quicktrain"


    env = my_env(**task_parameter)
    args.state_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    args.state_shape = args.state_shape[1:]
    args.action_shape = 5
    if args.reward_threshold is None:
        default_reward_threshold = {"pursuit_v4": 1000}
        args.reward_threshold = default_reward_threshold.get(
            args.task  # , env.spec.reward_threshold
        )
    # train_envs = gym.make(args.task)
    # you can also use tianshou.env.SubprocVectorEnv
    train_envs = SubprocVectorEnv(
        [lambda: my_env(**task_parameter) for _ in range(args.training_num)]
    )
    # test_envs = gym.make(args.task)
    test_envs = SubprocVectorEnv(
        [lambda: my_env(**task_parameter) for _ in range(args.test_num)]
    )
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    train_envs.seed(args.seed)
    test_envs.seed(args.seed)
    # model
    net = Net(args.state_shape, hidden_sizes=args.hidden_sizes, device=args.device)
    if torch.cuda.is_available() and False: # always don't use DataParallelNet until multi-gpu is configured
        actor = DataParallelNet(
            Actor(net, args.action_shape, device=None).to(args.device)
        )
        critic = DataParallelNet(Critic(net, device=None).to(args.device))
    else:
        actor = Actor(net, args.action_shape, device=args.device).to(args.device)
        critic = Critic(net, device=args.device).to(args.device)
    actor_critic = ActorCritic(actor, critic)
    # orthogonal initialization
    for m in actor_critic.modules():
        if isinstance(m, torch.nn.Linear):
            torch.nn.init.orthogonal_(m.weight)
            torch.nn.init.zeros_(m.bias)
    optim = torch.optim.Adam(actor_critic.parameters(), lr=args.lr)
    dist = torch.distributions.Categorical
    policy = myPPOPolicy(
        num_agents=task_parameter["n_pursuers"],
        state_shape=args.state_shape,
        device=args.device,
        actor=actor,
        critic=critic,
        optim=optim,
        dist_fn=dist,
        discount_factor=args.gamma,
        max_grad_norm=args.max_grad_norm,
        eps_clip=args.eps_clip,
        vf_coef=args.vf_coef,
        ent_coef=args.ent_coef,
        gae_lambda=args.gae_lambda,
        reward_normalization=args.rew_norm,
        dual_clip=args.dual_clip,
        value_clip=args.value_clip,
        action_space=env.action_space,
        deterministic_eval=True,
        advantage_normalization=args.norm_adv,
        recompute_advantage=args.recompute_adv
    )
    # collector
    train_collector = Collector(
        policy, train_envs, VectorReplayBuffer(args.buffer_size, len(train_envs))
    )
    test_collector = Collector(policy, test_envs)
    # train_collector.collect(n_step=args.batch_size * args.training_num)
    train_datetime = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # log
    log_path = os.path.join(args.logdir, args.task, "ppo", train_datetime)
    
    # logger and writer
    config = dict(
        args=vars(args),
        task_parameter=task_parameter,
        train_datetime=train_datetime,
        log_path=log_path,
    )
    logger = WandbLogger(project="pursuit_ppo", entity="csfyp", config=config, train_interval=int(1e5), update_interval=int(1e5))
    writer = SummaryWriter(log_path)
    writer.add_text("args", str(args))
    writer.add_text("env_para", str(task_parameter))
    writer.add_text("env_name", str(my_env))
    writer.add_text("date_time", train_datetime)
    # logger = TensorboardLogger(writer)
    logger.load(writer)
    print("config:")
    pprint.pprint(config)
    print("-" * 20)

    def save_best_fn(policy):
        torch.save(policy.state_dict(), os.path.join(log_path, "policy.pth"))

    def stop_fn(mean_rewards):
        return mean_rewards >= args.reward_threshold

    # trainer
    result = onpolicy_trainer(
        policy,
        train_collector,
        test_collector,
        args.epoch,
        args.step_per_epoch,
        args.repeat_per_collect,
        args.test_num,
        args.batch_size,
        step_per_collect=args.step_per_collect,
        stop_fn=stop_fn,
        save_best_fn=save_best_fn,
        logger=logger,
    )
    # assert stop_fn(result['best_reward'])

    if __name__ == "__main__":
        pprint.pprint(result)
        # Let's watch its performance!

        envs = DummyVectorEnv([lambda: my_env(**task_parameter)])

        policy.eval()
        collector = Collector(policy, envs)
        result = collector.collect(n_episode=1, render=None)
        rews, lens = result["rews"], result["lens"]
        print(f"Final reward: {rews.mean()}, length: {lens.mean()}")


if __name__ == "__main__":
    test_ppo(get_args())