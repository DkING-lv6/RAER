import os
import numpy as np
import torch
from torch.optim import Adam
from torch.nn import functional as F

from .sac import SAC
from discor.network import TwinnedStateActionFunction
from discor.utils import disable_gradients, soft_update, update_params
from .rlkit.torch.networks import FlattenMlp
import json

class DisCor(SAC):

    def __init__(self, state_dim, action_dim, device, gamma=0.99, nstep=1,
                 policy_lr=0.0003, q_lr=0.0003, entropy_lr=0.0003,
                 error_lr=0.0003, policy_hidden_units=[256, 256],
                 q_hidden_units=[256, 256], error_hidden_units=[256, 256, 256],
                 prob_hidden_units=[128, 128], prob_temperature=7.5, 
                 tau_init=10.0, target_update_coef=0.005, lfiw=False, tau_scale=1,
                 hard_tper_weight=0.4, log_interval=10, seed=0, discor=False, 
                 tper=False, log_dir=None, env=None, eval_tper=False,
                 use_backward_timestep=False, reweigh_type="hard",
                 reweigh_hyper=None):
        super().__init__(
            state_dim, action_dim, device, gamma, nstep, policy_lr, q_lr,
            entropy_lr, policy_hidden_units, q_hidden_units,
            target_update_coef, log_interval, seed, env, eval_tper, log_dir)

        self.discor = discor
        self.lfiw = lfiw
        self.tper = tper
        # Build error networks.
        if self.discor:
            self._online_error_net = TwinnedStateActionFunction(
                state_dim=state_dim,
                action_dim=action_dim,
                hidden_units=error_hidden_units
                ).to(device=self._device)
            self._target_error_net = TwinnedStateActionFunction(
                state_dim=state_dim,
                action_dim=action_dim,
                hidden_units=error_hidden_units
                ).to(device=self._device).eval()
            # Copy parameters of the learning network to the target network.
            self._target_error_net.load_state_dict(
                self._online_error_net.state_dict())
            # Disable gradient calculations of the target network.
            disable_gradients(self._target_error_net)

            self._error_optim = Adam(
                self._online_error_net.parameters(), lr=error_lr)
            self._tau1 = torch.tensor(
                tau_init, device=self._device, requires_grad=False)
            self._tau2 = torch.tensor(
                tau_init, device=self._device, requires_grad=False)

            if tau_init < 1e-6:
                self.no_tau = True
                print("===========No tau!==========")
            else:
                self.no_tau = False
            self.tau_scale = tau_scale

        if self.lfiw:
            self._prob_classifier = FlattenMlp(
                input_size=state_dim+action_dim,
                output_size=1,
                hidden_sizes=prob_hidden_units,
                ).to(device=self._device)
            self._prob_optim = Adam(
                self._prob_classifier.parameters(), lr=q_lr)
            self.prob_temperature = prob_temperature

        if self.tper:
            self.hard_tper_weight = hard_tper_weight
            self.use_backward_timestep = use_backward_timestep
            self.reweigh_type = reweigh_type
            self.reweigh_hyper = reweigh_hyper
            self.l, self.h, self.k, self.b = \
                [torch.tensor(i).to(device=self._device) for i in self.reweigh_hyper["linear"]]
            if self.reweigh_type in ["adaptive_linear", "done_cnt_linear"]:
                self.low_l, self.low_h, self.high_l, self.high_h, self.t_s, self.t_e = \
                    [torch.tensor(i).to(device=self._device) for i in self.reweigh_hyper["adaptive_linear"]]
            if "exp" in self.reweigh_type:
                self.exp_k, self.exp_gamma = self.reweigh_hyper["exp"]
        self.Qs = 2

        self._param_dir = os.path.join(log_dir, 'param')
        if not os.path.exists(self._param_dir):
            os.makedirs(self._param_dir)
        with open(os.path.join(self._param_dir, "discor_params.txt"), 'w') as f:
            for key, value in zip(locals().keys(), locals().values()):
                print(key, ":", value, file=f)

    def update_target_networks(self):
        super().update_target_networks()
        if self.discor:
            soft_update(
                self._target_error_net, self._online_error_net,
                self._target_update_coef)

    def update_online_networks(self, batch, writer):
        self._learning_steps += 1
        self.update_policy_and_entropy(batch['uniform'], writer)
        self.update_q_functions_and_error_models(batch, writer)

    def calc_update_d_pi_iw(self, slow_obs, slow_act, fast_obs, fast_act, fast_statistics, target_obs=None, target_act=None):
        slow_samples = torch.cat((slow_obs, slow_act), dim=1)
        fast_samples = torch.cat((fast_obs, fast_act), dim=1)

        zeros = torch.zeros(slow_samples.size(0)).to(device=self._device)
        ones = torch.ones(fast_samples.size(0)).to(device=self._device)
        # statistic = np.reshape(fast_statistics.cpu(), (1, -1))
        slow_preds = self._prob_classifier(slow_samples)
        fast_preds = self._prob_classifier(fast_samples)

        loss = F.binary_cross_entropy(F.sigmoid(slow_preds), zeros) + \
                F.binary_cross_entropy(F.sigmoid(fast_preds), fast_statistics)

        update_params(self._prob_optim, loss)

        # In case we want to compute ratio on data different from what we train the network
        if target_obs is None:
            target_obs = slow_obs
        if target_act is None:
            target_act = slow_act
        target_samples = torch.cat((target_obs, target_act), dim=1)
        slow_preds = self._prob_classifier(target_samples)

        importance_weights = F.sigmoid(slow_preds/self.prob_temperature).detach()
        importance_weights = importance_weights / torch.sum(importance_weights)

        return importance_weights, loss

    def update_q_functions_and_error_models(self, batch, writer):
        uniform_batch = batch["uniform"]
        if self.lfiw:
            fast_batch = batch['fast']
            fast_states, fast_actions, fast_statistics = fast_batch['states'], fast_batch['actions'], fast_batch['statistics']
        else:
            fast_batch = None
        # train_batch = batch["prior"] if self.tper else batch["uniform"]
        train_batch = batch["uniform"]
        
        # transition to update Q net
        states, actions, next_states, dones = \
            train_batch["states"], train_batch["actions"], train_batch["next_states"], train_batch["dones"]
        # s,a to update the weight of lfiw network
        slow_states, slow_actions = uniform_batch["states"], uniform_batch["actions"]

        # Calculate importance weights.
        batch_size = states.shape[0]
        weights1 = torch.ones((batch_size, 1)).to(device=self._device)
        weights2 = torch.ones((batch_size, 1)).to(device=self._device)
        if self.discor:
            discor_weights = self.calc_importance_weights(next_states, dones)
            # print(weights[0].shape, discor_weights[0].shape)
            weights1 *= discor_weights[0]
            weights2 *= discor_weights[1]
        # Calculate and update prob_classifier
        if self.lfiw:
            lfiw_weights, prob_loss = self.calc_update_d_pi_iw(slow_states, slow_actions, fast_states, fast_actions, fast_statistics, states, actions)
            weights1 *= lfiw_weights
            weights2 *= lfiw_weights
        # Calculate weights for temporal priority
        if self.tper:
            steps = train_batch["steps"]
            done_cnts = train_batch["done_cnts"]
            tper_weights = self.calc_tper_weights(steps, done_cnts)
            weights1 *= tper_weights
            weights2 *= tper_weights

        # Update Q functions.
        curr_errs1, curr_errs2 = None, None
        if self.discor:
            curr_errs1, curr_errs2 = self.calc_current_errors(states, actions)
        # pass in curr_errs1 for evaluating discor
        curr_qs1, curr_qs2, target_qs = \
            self.update_q_functions(train_batch, writer, weights1, weights2, fast_batch, curr_errs1)

        # Calculate current and target errors.
        if self.discor:
            target_errs1, target_errs2 = self.calc_target_errors(
                next_states, dones, curr_qs1, curr_qs2, target_qs)
            # Update error models.
            err_loss = self.calc_error_loss(
                curr_errs1, curr_errs2, target_errs1, target_errs2)
            update_params(self._error_optim, err_loss)
        
        if self._learning_steps % self._log_interval == 0:
            if self.discor:
                writer.add_scalar(
                    'loss/error', err_loss.detach().item(),
                    self._learning_steps)
                writer.add_scalar(
                    'stats/tau1', self._tau1.item(), self._learning_steps)
                writer.add_scalar(
                    'stats/tau2', self._tau2.item(), self._learning_steps)
            if self.lfiw:
                writer.add_scalar(
                    'loss/prob_loss', prob_loss.detach().item(),
                    self._learning_steps)

    def calc_tper_weights(self, steps, done_cnts):
        steps = steps.to(dtype=torch.float32) 
        rel_step = steps / torch.max(steps)
        if self.use_backward_timestep:
            # convert bk step to forward 
            rel_step = 1 - rel_step

        if self.reweigh_type == 'hard':
            assert self.hard_tper_weight <= 0.5
            med = torch.median(steps)
            one = torch.tensor(1-self.hard_tper_weight, device=self._device, requires_grad=False)
            zero = torch.tensor(self.hard_tper_weight, device=self._device, requires_grad=False)
            cond = steps < med if self.use_backward_timestep else steps > med
            weight = torch.where(cond, one, zero)
        elif self.reweigh_type == 'linear':
            weight = self._calc_linear_weight(rel_step, self.l, self.h, self.k, self.b)
        elif self.reweigh_type == 'adaptive_linear':
            cur_low = torch.clamp(
                self.low_l + (self.low_h - self.low_l)/(self.t_e - self.t_s)*(self._learning_steps - self.t_s), 
                self.low_l, 
                self.low_h
            )
            cur_high = torch.clamp(
                self.high_h + (self.high_l - self.high_h)/(self.t_e - self.t_s)*(self._learning_steps - self.t_s), 
                self.high_l, 
                self.high_h
            )
            weight = self._calc_linear_weight(rel_step, cur_low, cur_high, self.k, self.b)
        elif self.reweigh_type == 'done_cnt_linear':
            rel_done_cnt = done_cnts.to(dtype=torch.float32) / torch.max(done_cnts)
            # The tajectory is newer with larger done counts, which can be understood as fewer learning steps
            pseudo_step = 1 - rel_done_cnt
            cur_low = torch.clamp(
                self.low_l + (self.low_h - self.low_l) * pseudo_step,
                self.low_l, 
                self.low_h
            )
            cur_high = torch.clamp(
                self.high_h + (self.high_l - self.high_h) * pseudo_step,
                self.high_l, 
                self.high_h
            )
            weight = self._calc_linear_weight(rel_step, cur_low, cur_high, self.k, self.b)
        elif self.reweigh_type == 'exp':
            # compute exp weight with exp(k*gamma^bk_step)
            if not self.use_backward_timestep:
                # compute proxy backward step
                steps = torch.max(steps) - steps
            weight = torch.exp(self.exp_k * self.exp_gamma ** steps) / (2.71828 - 1)
            weight = weight / torch.sum(weight) * steps.shape[0]
        return weight

    def _calc_linear_weight(self, rel_step, l, h, k, b):
        assert torch.max(rel_step) <= 1
        assert torch.min(rel_step) >= 0
        weight = torch.max(torch.min(k*rel_step+b, h), l)
        # weight = torch.clamp(k*rel_step+b, l, h)
        weight = weight / torch.sum(weight) * rel_step.shape[0]

        return weight

    def calc_importance_weights(self, next_states, dones):
        with torch.no_grad():
            next_actions, _, _ = self._policy_net(next_states)
            next_errs1, next_errs2 = \
                self._target_error_net(next_states, next_actions)

        # Terms inside the exponent of importance weights.
        if self.no_tau:
            x1 = -(1.0 - dones) * self._gamma * next_errs1
            x2 = -(1.0 - dones) * self._gamma * next_errs2
        else:
            x1 = -(1.0 - dones) * self._gamma * next_errs1 / (self._tau1 * self.tau_scale)
            x2 = -(1.0 - dones) * self._gamma * next_errs2 / (self._tau2 * self.tau_scale)


        # Calculate self-normalized importance weights.
        imp_ws1 = F.softmax(x1, dim=0)
        imp_ws2 = F.softmax(x2, dim=0)

        return imp_ws1, imp_ws2

    def calc_current_errors(self, states, actions):
        curr_errs1, curr_errs2 = self._online_error_net(states, actions)
        return curr_errs1, curr_errs2

    def calc_target_errors(self, next_states, dones, curr_qs1, curr_qs2,
                           target_qs):
        # Calculate targets of the cumulative sum of discounted Bellman errors,
        # which is 'Delta' in the paper.
        with torch.no_grad():
            next_actions, _, _ = self._policy_net(next_states)
            next_errs1, next_errs2 = \
                self._target_error_net(next_states, next_actions)

            target_errs1 = (curr_qs1 - target_qs).abs() + \
                (1.0 - dones) * self._gamma * next_errs1
            target_errs2 = (curr_qs2 - target_qs).abs() + \
                (1.0 - dones) * self._gamma * next_errs2

        return target_errs1, target_errs2

    def calc_error_loss(self, curr_errs1, curr_errs2, target_errs1,
                        target_errs2):
        err1_loss = torch.mean((curr_errs1 - target_errs1).pow(2))
        err2_loss = torch.mean((curr_errs2 - target_errs2).pow(2))

        soft_update(
            self._tau1, curr_errs1.detach().mean(), self._target_update_coef)
        soft_update(
            self._tau2, curr_errs2.detach().mean(), self._target_update_coef)

        return err1_loss + err2_loss

    def save_models(self, save_dir):
        super().save_models(save_dir)
        if self.discor:
            self._online_error_net.save(
                os.path.join(save_dir, 'online_error_net.pth'))
            self._target_error_net.save(
                os.path.join(save_dir, 'target_error_net.pth'))
        if self.lfiw:
            self._prob_classifier.save(
                os.path.join(save_dir, 'prob_classifier.pth'))
