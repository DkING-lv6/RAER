import os
from algorithm.base import Algorithm
import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter

from discor.replay_buffer import ReplayBuffer, TemporalPrioritizedReplayBuffer, BackTimeBuffer
from discor.utils import RunningMeanStats


class Agent:
    global DataCsv
    DataCsv = np.zeros([1, 400])
    global d
    global M
    M = 50

    def __init__(self, env, test_env, algo: Algorithm, log_dir, device, num_steps=3000000,
                 batch_size=256, memory_size=1000000, fast_memory_size=None,
                 update_interval=1, start_steps=10000, log_interval=10, use_tper=False,
                 eval_interval=5000, num_eval_episodes=5, seed=0, use_backward_steps=False,
                 save_model_interval=0, eval_tper=False,
                 ):

        # Environment.
        self._env = env
        self._test_env = test_env

        self._env.seed(seed)
        self._test_env.seed(2**31-1-seed)

        # Algorithm.
        self._algo = algo

        self.tper = True if use_tper else False

        # Replay buffer with n-step return.
        buffer = TemporalPrioritizedReplayBuffer if use_tper else ReplayBuffer
        if use_backward_steps:
            buffer = BackTimeBuffer
        self._replay_buffer = buffer(
            memory_size=memory_size,
            state_shape=self._env.observation_space.shape,
            action_shape=self._env.action_space.shape,
            gamma=self._algo.gamma, nstep=self._algo.nstep,
            arbi_reset=eval_tper)

        if hasattr(algo, "lfiw") and algo.lfiw:
            if not fast_memory_size:
                # fast_memory_size = memory_size // 10
                fast_memory_size = self._env._max_episode_steps * M
            self.lfiw = True
            self._fast_replay_buffer = buffer(
                memory_size=fast_memory_size,
                state_shape=self._env.observation_space.shape,
                action_shape=self._env.action_space.shape,
                gamma=self._algo.gamma, nstep=self._algo.nstep,
                )
        else:
            self.lfiw = False

        # Directory to log.
        self._log_dir = log_dir
        self._model_dir = os.path.join(log_dir, 'model')
        self._summary_dir = os.path.join(log_dir, 'summary')
        self._stats_dir = os.path.join(log_dir, 'stats')
        if not os.path.exists(self._model_dir):
            os.makedirs(self._model_dir)
        if not os.path.exists(self._summary_dir):
            os.makedirs(self._summary_dir)
        if not os.path.exists(self._stats_dir):
            os.makedirs(self._stats_dir)

        self._steps = 0
        self._episodes = 0
        Total_episodes = int(num_steps / self._env._max_episode_steps)
        self._train_return = RunningMeanStats(num_steps)
        self._train_P = RunningMeanStats(num_steps)
        self._train_statistic = RunningMeanStats(M)
        self._writer = SummaryWriter(log_dir=self._summary_dir)
        self._best_eval_score = -np.inf

        self._device = device
        self._num_steps = num_steps
        self._batch_size = batch_size
        if self.tper:
            self._batch_size *= 2 # sample twice and select the half with larger steps
        self._update_interval = update_interval
        self._start_steps = start_steps
        self._log_interval = log_interval
        self._eval_interval = eval_interval
        self._num_eval_episodes = num_eval_episodes

        self._save_model_interval = save_model_interval
        self._eval_tper = eval_tper
        
    def run(self):
        statistic_threshold = 0
        while True:
            sta_thre = self.train_episode(statistic_threshold)
            statistic_threshold = sta_thre
            if self._steps > self._num_steps:
                np.savetxt('Data2.csv', DataCsv, delimiter=',')
                break

    def train_episode(self, statistic_threshold=None):
        self._episodes += 1
        episode_return = 0.
        episode_steps = 0
        statistic = 0

        done = False
        state = self._env.reset()
        temp = []

        while (not done):

            if self._start_steps > self._steps:
                action = self._env.action_space.sample()
            else:
                action = self._algo.explore(state)

            if self._eval_tper:
                sim_state = self._env.sim.get_state()
            next_state, reward, done, _ = self._env.step(action)

            # Set done=True only when the agent fails, ignoring done signal
            # if the agent reach time horizons.
            if episode_steps + 1 >= self._env._max_episode_steps:
                masked_done = False
                done = True
            else:
                masked_done = done

            transition = [state, action, reward, next_state, masked_done, episode_steps, done, statistic]
            if self._eval_tper:
                transition.append(sim_state)

            self._replay_buffer.append(*transition)
            tran = np.array(transition)
            temp.append(tran)
            # if self.lfiw:
            #     self._fast_replay_buffer.append(*transition)

            self._steps += 1
            episode_steps += 1
            episode_return += reward
            state = next_state

            if self._steps >= self._start_steps:
                # Update online networks.
                if self._steps % self._update_interval == 0:
                    batch = {}
                    uniform_batch = self._replay_buffer.sample(
                        self._batch_size, self._device)
                    batch.update({"uniform": uniform_batch})
                    # if self.tper:
                    #     prior_batch = self._replay_buffer.prior_sample(
                    #         self._batch_size, self._device)
                    #     batch.update({"prior": prior_batch})
                    if self.lfiw:
                        fast_batch = self._fast_replay_buffer.sample(
                            self._batch_size, self._device)
                        zero = torch.zeros_like(fast_batch['statistics'])
                        fast_batch['statistics'] = torch.where(fast_batch['statistics'] < statistic_threshold, zero, fast_batch['statistics'])
                        batch.update({"fast": fast_batch})
                    self._algo.update_online_networks(batch, self._writer)


                # Update target networks.
                self._algo.update_target_networks()

                # Evaluate.
                if self._steps % self._eval_interval == 0:
                    d = int(self._steps / self._eval_interval - 1)
                    self.evaluate(d)
                    if self._save_model_interval == 0:
                        self._algo.save_models(
                            os.path.join(self._model_dir, 'final'))
                    else:
                        if self._steps % self._save_model_interval == 0 and self._steps != 0:
                            self._algo.save_models(
                                os.path.join(self._model_dir, '%dk'%(self._steps // 1000)))

        # We log running mean of training rewards.
        self._train_return.append(episode_return)
        temp_R = 0
        if self._episodes > 1:
            if self._episodes > M:
                for m in range(M):
                    if episode_return > self._train_return._stats[self._episodes - m - 2]:
                        temp_R += 1
            else:
                for j in range(self._episodes - 1):
                    if episode_return > self._train_return._stats[self._episodes - j - 2]:
                        temp_R += 1
            self._train_P.append(temp_R)
            if self._episodes <= M:
                episode_statistic = self._train_P._stats[self._episodes - 1] / (self._episodes - 1)  # before M
            elif self._episodes <= M * 2:
                episode_statistic = self._train_P._stats[self._episodes - 1] / M  # before 2M
            else:
                temp_Sta = 0
                for n in range(M):
                    temp_Sta += self._train_P._stats[self._episodes - n - 1]
                episode_statistic = temp_Sta / (M ** 2)
        else:
            self._train_P.append(temp_R)
            episode_statistic = 0

        for i in range(len(temp)):
            tran = temp[i].tolist()
            tran[7] = episode_statistic
            self._fast_replay_buffer.append(*tran)

        # We log training statistics
        self._train_statistic.append(episode_statistic)
        statistic_threshold = 0.1
        # if self._episodes % self._log_interval == 0:
        #     self._writer.add_scalar(
        #         'reward/train', self._train_return.get(), self._steps)

        print(f'Episode: {self._episodes:<4}  '
              f'Episode steps: {episode_steps:<4}  '
              f'Return: {episode_return:<5.1f}')
        return statistic_threshold

    def evaluate(self, d):
        total_return = 0.0
        if self._test_env.is_metaworld:
            total_success = 0.0

        for _ in range(self._num_eval_episodes):
            state = self._test_env.reset()
            episode_return = 0.0
            done = False
            if self._test_env.is_metaworld:
                success = 0.0

            episode_steps = 0
            while (not done):
                action = self._algo.exploit(state)
                next_state, reward, done, info = self._test_env.step(action)
                episode_steps += 1
                if episode_steps + 1 >= self._env._max_episode_steps:
                    done = True
                episode_return += reward
                state = next_state

                if self._test_env.is_metaworld and info['success'] > 1e-8:
                    success = 1.0

            total_return += episode_return
            if self._test_env.is_metaworld:
                total_success += success

        mean_return = total_return / self._num_eval_episodes
        if self._test_env.is_metaworld:
            success_rate = total_success / self._num_eval_episodes
            # self._writer.add_scalar(
            #     'reward/success_rate', success_rate, self._steps)

        if mean_return > self._best_eval_score:
            self._best_eval_score = mean_return
            self._algo.save_models(os.path.join(self._model_dir, 'best'))

        # self._writer.add_scalar(
        #     'reward/test', mean_return, self._steps)
        print('-' * 60)
        print(f'Num steps: {self._steps:<5}  '
              f'return: {mean_return:<5.1f}')
        print('-' * 60)
        DataCsv[0, d] = mean_return

    def __del__(self):
        self._env.close()
        self._test_env.close()
        # self._writer.close()
