import torch as ch
import numpy as np
import time
from tqdm import tqdm
from torch.nn.utils import parameters_to_vector as flatten
from torch.nn.utils import vector_to_parameters as assign
from .torch_utils import *
import matplotlib as mpl
mpl.use('Agg')  # No display
import matplotlib.pyplot as plt
from .convex_relaxation import get_kl_bound as get_state_kl_bound

'''
File for taking steps in both policy and value network space.
Layout of this file:
    - Surrogate reward function
    - Logging functions for TRPO approximations
        - kl_approximation_logging
        - kl_vs_second_order_approx
    - Possible value loss functions
        - consistency loss [+ clipped version for matching OpenAI]
        - time-dependent baseline
    - Actual optimization functions
        - value_step
        - ppo_step
        - trpo_step
'''
def adv_normalize(adv):
    std = adv.std()
    
    assert std != 0. and not ch.isnan(std), 'Need nonzero std'
    n_advs = (adv - adv.mean())/(adv.std()+1e-8)
    return n_advs

def surrogate_reward(adv, *, new, old, clip_eps=None):
    '''
    Computes the surrogate reward for TRPO and PPO:
    R(\theta) = E[r_t * A_t]
    with support for clamping the ratio (for PPO), s.t.
    R(\theta) = E[clamp(r_t, 1-e, 1+e) * A_t]
    Inputs:
    - adv, unnormalized advantages as calculated by the agents
    - log_ps_new, the log probabilities assigned to taken events by \theta_{new}
    - log_ps_old, the log probabilities assigned to taken events by \theta_{old}
    - clip_EPS, the clipping boundary for PPO loss
    Returns:
    - The surrogate loss as described above
    '''
    log_ps_new, log_ps_old = new, old

    # Normalized Advantages
    n_advs = adv_normalize(adv)

    assert shape_equal_cmp(log_ps_new, log_ps_old, n_advs)

    # Ratio of new probabilities to old ones
    ratio_new_old = ch.exp(log_ps_new - log_ps_old)

    # Clamping (for use with PPO)
    if clip_eps is not None:
        ratio_new_old = ch.clamp(ratio_new_old, 1-clip_eps, 1+clip_eps)
    return ratio_new_old * n_advs

######
# Possible Loss Functions for the value network
# Supports consistency loss, time-dependent baseline, OpenAI loss
# Also logs explained variance = MSE(values, targets)/Var[targets]
#####

def value_loss_gae(vs, _, advantages, not_dones, params, old_vs, store=None, re=False):
    '''
    GAE-based loss for the value function:
        L_t = ((v_t + A_t).detach() - v_{t})
    Optionally, we clip the value function around the original value of v_t

    Inputs: rewards, returns, not_dones, params (from value_step)
    Outputs: value function loss
    '''
    # Desired values are old values plus advantage of the action taken. They do not change during the optimization process.
    # We want the current values are close to them.
    val_targ = (old_vs + advantages).detach()
    assert shape_equal_cmp(val_targ, vs, not_dones, old_vs, advantages)
    assert len(vs.shape) == 1

    try:
        vs_clipped = old_vs + ch.clamp(vs - old_vs, -params.CLIP_VAL_EPS, params.CLIP_VAL_EPS)
    except AttributeError as e:
        vs_clipped = old_vs + ch.clamp(vs - old_vs, -params.CLIP_EPS, params.CLIP_EPS)
        
    # Don't incur loss from last timesteps (since there's no return to use)
    sel = not_dones.bool()
    val_loss_mat_unclipped = (vs - val_targ)[sel].pow(2)
    val_loss_mat_clipped = (vs_clipped - val_targ)[sel].pow(2)

    # In OpenAI's PPO implementation, we clip the value function around the previous value estimate
    # and use the worse of the clipped and unclipped versions to train the value function

    # Presumably the inspiration for this is similar to PPO
    if params.VALUE_CLIPPING:
        val_loss_mat = ch.max(val_loss_mat_unclipped, val_loss_mat_clipped)
    else:
        val_loss_mat = val_loss_mat_unclipped

    # assert shape_equal_cmp(val_loss_mat, vs)
    # Mean squared loss
    mse = val_loss_mat.mean()

    if re:
        # Relative error.
        se = not_dones.bool()
        relerr = val_loss_mat/val_targ[se].abs()
        mre = relerr.abs().mean()
        msre = relerr.pow(2).mean()
        return mse, mre, msre

    return mse

def value_loss_returns(vs, returns, advantages, not_dones, params, old_vs,
                       store=None, re=False):
    '''
    Returns (with time input) loss for the value function:
        L_t = (R_t - v(s, t))
    Inputs: rewards, returns, not_dones, params (from value_step)
    Outputs: value function loss
    '''
    assert shape_equal_cmp(vs, returns)
    sel = not_dones.bool()
    val_loss_mat = (vs - returns)[sel]
    mse = val_loss_mat.pow(2).mean()
    val_targ = returns

    if re:
        relerr = val_loss_mat/val_targ[sel].abs()
        mre = relerr.abs().mean()
        msre = relerr.pow(2).mean()
        return mse, mre, msre

    return mse

###
# Optimization functions for the value and policy parameters
# value_step, ppo_step, trpo_step
###
def value_step(all_states, returns, advantages, not_dones, net,
               val_opt, params, store, old_vs=None, opt_step=None,
               should_tqdm=False, should_cuda=False, test_saps=None):
    '''
    Take an optimizer step fitting the value function
    parameterized by a neural network
    Inputs:
    - all_states, the states at each timestep
    - rewards, the rewards gained at each timestep
    - returns, discounted rewards (ret_t = r_t + gamma*ret_{t+1})
    - advantaages, estimated by GAE
    - not_dones, N * T array with 0s at final steps and 1s everywhere else
    - net, the neural network representing the value function 
    - val_opt, the optimizer for net
    - params, dictionary of parameters
    Returns:
    - Loss of the value regression problem
    '''

    # (sharing weights) XOR (old_vs is None)
    assert params.SHARE_WEIGHTS ^ (old_vs is None)

    # Options for value function
    VALUE_FUNCS = {
        "gae": value_loss_gae,
        "time": value_loss_returns
    }
         
    # If we are not sharing weights, then we need to keep track of what the 
    # last value was here. If we are sharing weights, this is handled in policy_step
    with ch.no_grad():
        if old_vs is None:
            state_indices = np.arange(returns.nelement())
            # No shuffling, just split an sequential list of indices.
            splits = np.array_split(state_indices, params.NUM_MINIBATCHES)
            orig_vs = []
            # Minibatch.
            for selected in splits:
                # Values of current network prediction.
                orig_vs.append(net(all_states[selected]).squeeze(-1))
            orig_vs = ch.cat(orig_vs)
            old_vs = orig_vs.detach()
        if test_saps is not None:
            old_test_vs = net(test_saps.states).squeeze(-1)

    r = range(params.VAL_EPOCHS) if not should_tqdm else \
                            tqdm(range(params.VAL_EPOCHS))
    for i in r:
        # Create minibatches with shuffuling
        state_indices = np.arange(returns.nelement())
        np.random.shuffle(state_indices)
        splits = np.array_split(state_indices, params.NUM_MINIBATCHES)

        assert shape_equal_cmp(returns, advantages, not_dones, old_vs)

        # Minibatch SGD
        for selected in splits:
            val_opt.zero_grad()

            def sel(*args):
                return [v[selected] for v in args]

            def to_cuda(*args):
                return [v.cuda() for v in args]

            # Get a minibatch (64) of returns, advantages, etc.
            tup = sel(returns, advantages, not_dones, old_vs, all_states)
            if should_cuda: tup = to_cuda(*tup)
            sel_rets, sel_advs, sel_not_dones, sel_ovs, sel_states = tup
            # Value prediction of current network given the states.
            vs = net(sel_states).squeeze(-1)
            assert shape_equal_cmp(vs, selected)

            vf = VALUE_FUNCS[params.VALUE_CALC]
            val_loss = vf(vs, sel_rets, sel_advs, sel_not_dones, params,
                          sel_ovs, store)

            # If we are sharing weights, then value_step gets called 
            # once per policy optimizer step anyways, so we only do one batch
            if params.SHARE_WEIGHTS:
                return val_loss

            # From now on, params.SHARE_WEIGHTS must be False
            val_loss.backward()
            val_opt.step()
        if should_tqdm:
            if test_saps is not None: 
                vs = net(test_saps.states).squeeze(-1)
                test_loss = vf(vs, test_saps.returns, test_saps.advantages,
                    test_saps.not_dones, params, old_test_vs, None)
            r.set_description(f'vf_train: {val_loss.mean().item():.2f}'
                              f'vf_test: {test_loss.mean().item():.2f}')

    return val_loss

def ppo_step(all_states, actions, old_log_ps, rewards, returns, not_dones, 
                advs, net, params, store, opt_step):
    '''
    Proximal Policy Optimization
    Runs K epochs of PPO as in https://arxiv.org/abs/1707.06347
    Inputs:
    - all_states, the historical value of all the states
    - actions, the actions that the policy sampled
    - old_log_ps, the log probability of the actions that the policy sampled
    - advs, advantages as estimated by GAE
    - net, policy network to train [WILL BE MUTATED]
    - params, additional placeholder for parameters like EPS
    Returns:
    - The PPO loss; main job is to mutate the net
    '''
    # Storing batches of stuff
    # if store is not None:
    #     orig_dists = net(all_states)

    ### ACTUAL PPO OPTIMIZATION START
    if params.SHARE_WEIGHTS:
        orig_vs = net.get_value(all_states).squeeze(-1).view([params.NUM_ACTORS, -1])
        old_vs = orig_vs.detach()

    for _ in range(params.PPO_EPOCHS):
        # State is in shape (experience_size, observation_size). Usually 2048.
        state_indices = np.arange(all_states.shape[0])
        np.random.shuffle(state_indices)
        # We use a minibatch of states to do optimization, and each epoch contains several iterations.
        splits = np.array_split(state_indices, params.NUM_MINIBATCHES)
        # A typical mini-batch size is 2048/32=64
        for selected in splits:
            def sel(*args):
                return [v[selected] for v in args]

            # old_log_ps: log probabilities of actions sampled based in experience buffer.
            # advs: advantages of these states.
            # both old_log_ps and advs are in shape (experience_size,) = 2048.
            tup = sel(all_states, actions, old_log_ps, advs)
            # select log probabilities, advantages of this minibatch.
            batch_states, batch_actions, batch_old_log_ps, batch_advs = tup

            # Forward propagation on current parameters (being constantly updated), to get distribution of these states
            # dist contains mean and variance of Gaussian.
            dist = net(batch_states)
            # Convert state distribution to log likelyhood.
            new_log_ps = net.get_loglikelihood(dist, batch_actions)

            shape_equal_cmp(new_log_ps, batch_old_log_ps)

            # Calculate rewards
            # the surrogate rewards is basically exp(new_log_ps - old_log_ps) * advantage
            # dimension is the same as minibatch size.
            unclp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps)
            clp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps,
                                       clip_eps=params.CLIP_EPS)

            # Calculate entropy bonus
            entropy_bonus = net.entropies(dist).mean()

            # Total loss, is the min of clipped and unclipped reward for each state, averaged.
            surrogate = -ch.min(unclp_rew, clp_rew).mean()
            entropy = -params.ENTROPY_COEFF * entropy_bonus
            loss = surrogate + entropy
            
            # If we are sharing weights, take the value step simultaneously 
            # (since the policy and value networks depend on the same weights)
            if params.SHARE_WEIGHTS:
                tup = sel(returns, not_dones, old_vs)
                batch_returns, batch_not_dones, batch_old_vs = tup
                val_loss = value_step(batch_states, batch_returns, batch_advs,
                                      batch_not_dones, net.get_value, None, params,
                                      store, old_vs=batch_old_vs, opt_step=opt_step)
                loss += params.VALUE_MULTIPLIER * val_loss

            # Optimizer step (Adam or SGD)
            if params.POLICY_ADAM is None:
                grad = ch.autograd.grad(loss, net.parameters())
                flat_grad = flatten(grad)
                if params.CLIP_GRAD_NORM != -1:
                    norm_grad = ch.norm(flat_grad)
                    flat_grad = flat_grad if norm_grad <= params.CLIP_GRAD_NORM else \
                                flat_grad / norm_grad * params.CLIP_GRAD_NORM

                assign(flatten(net.parameters()) - params.PPO_LR * flat_grad, net.parameters())
            else:
                params.POLICY_ADAM.zero_grad()
                loss.backward()
                if params.CLIP_GRAD_NORM != -1:
                    ch.nn.utils.clip_grad_norm(net.parameters(), params.CLIP_GRAD_NORM)
                params.POLICY_ADAM.step()
        print(f'surrogate={surrogate.item():8.5f}, entropy={entropy_bonus.item():8.5f}, loss={loss.item():8.5f}')

    std = ch.exp(net.log_stdev)
    print(f'std_min={std.min().item():8.5f}, std_max={std.max().item():8.5f}, std_mean={std.mean().item():8.5f}')


    return loss


"""Computing an estimated upper bound of KL divergence using SGLD."""
def get_state_kl_bound_sgld(net, batch_states, batch_action_means, eps, steps, stdev):
    batch_action_means = batch_action_means.detach()
    # upper and lower bounds for clipping
    states_ub = batch_states + eps
    states_lb = batch_states - eps
    step_eps = eps / steps
    # SGLD noise factor. We set (inverse) beta=1e-5 as gradients are relatively small here.
    beta = 1e-5
    noise_factor = np.sqrt(2 * step_eps * beta)
    noise = ch.randn_like(batch_states) * noise_factor
    var_states = (batch_states.clone() + noise.sign() * step_eps).detach().requires_grad_()
    for i in range(steps):
        # Find a nearby state new_phi that maximize the difference
        diff = (net(var_states)[0] - batch_action_means) / stdev.detach()
        kl = (diff * diff).sum(axis=-1, keepdim=True).mean()
        # Need to clear gradients before the backward() for policy_loss
        kl.backward()
        # Reduce noise at every step.
        noise_factor = np.sqrt(2 * step_eps * beta) / (i+2)
        # Project noisy gradient to step boundary.
        update = (var_states.grad + noise_factor * ch.randn_like(var_states)).sign() * step_eps
        var_states.data += update
        # clip into the upper and lower bounds
        var_states = ch.max(var_states, states_lb)
        var_states = ch.min(var_states, states_ub)
        var_states = var_states.detach().requires_grad_()
    net.zero_grad()
    diff = (net(var_states.requires_grad_(False))[0] - batch_action_means) / stdev
    return (diff * diff).sum(axis=-1, keepdim=True)


def robust_ppo_step(all_states, actions, old_log_ps, rewards, returns, not_dones, 
                advs, net, params, store, opt_step, relaxed_net, eps_scheduler, beta_scheduler):
    '''
    Proximal Policy Optimization with robustness regularizer
    Runs K epochs of PPO as in https://arxiv.org/abs/1707.06347
    Inputs:
    - all_states, the historical value of all the states
    - actions, the actions that the policy sampled
    - old_log_ps, the log probability of the actions that the policy sampled
    - advs, advantages as estimated by GAE
    - net, policy network to train [WILL BE MUTATED]
    - params, additional placeholder for parameters like EPS
    Returns:
    - The PPO loss; main job is to mutate the net
    '''
    # Storing batches of stuff
    # if store is not None:
    #     orig_dists = net(all_states)

    ### ACTUAL PPO OPTIMIZATION START
    if params.SHARE_WEIGHTS:
        orig_vs = net.get_value(all_states).squeeze(-1).view([params.NUM_ACTORS, -1])
        old_vs = orig_vs.detach()

    # We treat all PPO epochs as one epoch.
    eps_scheduler.set_epoch_length(params.PPO_EPOCHS * params.NUM_MINIBATCHES)
    beta_scheduler.set_epoch_length(params.PPO_EPOCHS * params.NUM_MINIBATCHES)
    # We count from 1.
    eps_scheduler.step_epoch()
    beta_scheduler.step_epoch()

    for _ in range(params.PPO_EPOCHS):
        # State is in shape (experience_size, observation_size). Usually 2048.
        state_indices = np.arange(all_states.shape[0])
        np.random.shuffle(state_indices)
        # We use a minibatch of states to do optimization, and each epoch contains several iterations.
        splits = np.array_split(state_indices, params.NUM_MINIBATCHES)
        # A typical mini-batch size is 2048/32=64
        for selected in splits:
            def sel(*args):
                return [v[selected] for v in args]

            # old_log_ps: log probabilities of actions sampled based in experience buffer.
            # advs: advantages of these states.
            # both old_log_ps and advs are in shape (experience_size,) = 2048.
            tup = sel(all_states, actions, old_log_ps, advs)
            # select log probabilities, advantages of this minibatch.
            batch_states, batch_actions, batch_old_log_ps, batch_advs = tup

            # Forward propagation on current parameters (being constantly updated), to get distribution of these states
            # dist contains mean and variance of Gaussian.
            dist = net(batch_states)
            # Convert state distribution to log likelyhood.
            new_log_ps = net.get_loglikelihood(dist, batch_actions)

            shape_equal_cmp(new_log_ps, batch_old_log_ps)

            # Calculate rewards
            # the surrogate rewards is basically exp(new_log_ps - old_log_ps) * advantage
            # dimension is the same as minibatch size.
            unclp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps)
            clp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps,
                                       clip_eps=params.CLIP_EPS)

            # Calculate entropy bonus
            entropy_bonus = net.entropies(dist).mean()

            # Calculate regularizer under state perturbation.
            eps_scheduler.step_batch()
            beta_scheduler.step_batch()
            batch_action_means = dist[0]
            current_eps = eps_scheduler.get_eps()
            stdev = ch.exp(net.log_stdev)
            if params.ROBUST_PPO_DETACH_STDEV:
                # Detach stdev so that it won't be too large.
                stdev = stdev.detach()
            if params.ROBUST_PPO_METHOD == "convex-relax":
                kl_upper_bound = get_state_kl_bound(relaxed_net, batch_states, batch_action_means,
                        eps=current_eps, beta=beta_scheduler.get_eps(),
                        stdev=stdev).mean()
            elif params.ROBUST_PPO_METHOD == "sgld":
                kl_upper_bound = get_state_kl_bound_sgld(net, batch_states, batch_action_means,
                        eps=current_eps, steps=params.ROBUST_PPO_PGD_STEPS,
                        stdev=stdev).mean()
            else:
                raise ValueError(f"Unsupported robust PPO method {params.ROBUST_PPO_METHOD}")

            # Total loss, is the min of clipped and unclipped reward for each state, averaged.
            surrogate = -ch.min(unclp_rew, clp_rew).mean()
            entropy = -params.ENTROPY_COEFF * entropy_bonus
            loss = surrogate + entropy + params.ROBUST_PPO_REG * kl_upper_bound
            
            # If we are sharing weights, take the value step simultaneously 
            # (since the policy and value networks depend on the same weights)
            if params.SHARE_WEIGHTS:
                tup = sel(returns, not_dones, old_vs)
                batch_returns, batch_not_dones, batch_old_vs = tup
                val_loss = value_step(batch_states, batch_returns, batch_advs,
                                      batch_not_dones, net.get_value, None, params,
                                      store, old_vs=batch_old_vs, opt_step=opt_step)
                loss += params.VALUE_MULTIPLIER * val_loss

            # Optimizer step (Adam or SGD)
            if params.POLICY_ADAM is None:
                grad = ch.autograd.grad(loss, net.parameters())
                flat_grad = flatten(grad)
                if params.CLIP_GRAD_NORM != -1:
                    norm_grad = ch.norm(flat_grad)
                    flat_grad = flat_grad if norm_grad <= params.CLIP_GRAD_NORM else \
                                flat_grad / norm_grad * params.CLIP_GRAD_NORM

                assign(flatten(net.parameters()) - params.PPO_LR * flat_grad, net.parameters())
            else:
                params.POLICY_ADAM.zero_grad()
                loss.backward()
                if params.CLIP_GRAD_NORM != -1:
                    ch.nn.utils.clip_grad_norm(net.parameters(), params.CLIP_GRAD_NORM)
                params.POLICY_ADAM.step()
        # Logging.
        kl_upper_bound = kl_upper_bound.item()
        surrogate = surrogate.item()
        entropy_bonus = entropy_bonus.item()
        print(f'eps={eps_scheduler.get_eps():8.6f}, beta={beta_scheduler.get_eps():8.6f}, kl={kl_upper_bound:8.5f}, '
              f'surrogate={surrogate:8.5f}, entropy={entropy_bonus:8.5f}, loss={loss.item():8.5f}')
    std = ch.exp(net.log_stdev)
    print(f'std_min={std.min().item():8.5f}, std_max={std.max().item():8.5f}, std_mean={std.mean().item():8.5f}')

    if store is not None:
        row ={
            'eps': eps_scheduler.get_eps(),
            'beta': beta_scheduler.get_eps(),
            'kl': kl_upper_bound,
            'surrogate': surrogate,
            'entropy': entropy_bonus,
            'loss': loss.item(),
        }
        store.log_table_and_tb('robust_ppo_data', row)

    return loss

def trpo_step(all_states, actions, old_log_ps, rewards, returns, not_dones, advs, net, params, store, opt_step):
    '''
    Trust Region Policy Optimization
    Runs K epochs of TRPO as in https://arxiv.org/abs/1502.05477
    Inputs:
    - all_states, the historical value of all the states
    - actions, the actions that the policy sampled
    - old_log_ps, the probability of the actions that the policy sampled
    - advs, advantages as estimated by GAE
    - net, policy network to train [WILL BE MUTATED]
    - params, additional placeholder for parameters like EPS
    Returns:
    - The TRPO loss; main job is to mutate the net
    '''    
    # Initial setup
    initial_parameters = flatten(net.parameters()).clone()
    # all_states is in shape (experience_size, observation_size). Usually 2048 experiences.
    # Get mean and std of action distribution for all experiences.
    pds = net(all_states)
    # And compute the log probabilities for the actions chosen at rollout time.
    action_log_probs = net.get_loglikelihood(pds, actions)

    # Calculate losses
    surr_rew = surrogate_reward(advs, new=action_log_probs, old=old_log_ps).mean()
    grad = ch.autograd.grad(surr_rew, net.parameters(), retain_graph=True)
    # This represents the computation of gradient, and will be used to obtain 2nd order.
    flat_grad = flatten(grad)

    # Make fisher product estimator. Only use a fraction of examples.
    num_samples = int(all_states.shape[0] * params.FISHER_FRAC_SAMPLES)
    selected = np.random.choice(range(all_states.shape[0]), num_samples, replace=False)
    
    detached_selected_pds = select_prob_dists(pds, selected, detach=True)
    selected_pds = select_prob_dists(pds, selected, detach=False)
    
    # Construct the KL divergence which we will optimize on. This is essentially 0, but what we care about is the Hessian.
    # We want to know when the network parameter changes, how the K-L divergence of network output changes.
    kl = net.calc_kl(detached_selected_pds, selected_pds).mean()
    # g is the gradient of the KL divergence w.r.t to parameters. It is 0 at the starting point.
    g = flatten(ch.autograd.grad(kl, net.parameters(), create_graph=True))
    '''
    Fisher matrix to vector x product. Essentially, a Hessian-vector product of K-L divergence w.r.t network parameter.
    '''
    def fisher_product(x, damp_coef=1.):
        contig_flat = lambda q: ch.cat([y.contiguous().view(-1) for y in q])
        # z is the gradient-vector product. Take the derivation of it to get Hessian vector product.
        z = g @ x
        hv = ch.autograd.grad(z, net.parameters(), retain_graph=True)
        return contig_flat(hv).detach() + x*params.DAMPING * damp_coef

    # Find KL constrained gradient step
    # The Fisher matrix A is unknown, but we can compute the product.
    # flat_grad is the right-hand side value b. Want to solve x in Ax = b
    step = cg_solve(fisher_product, flat_grad, params.CG_STEPS)
    # Return the solution. "step" has size of network parameters.

    max_step_coeff = (2 * params.MAX_KL / (step @ fisher_product(step)))**(0.5)
    max_trpo_step = max_step_coeff * step

    if store and params.SHOULD_LOG_KL:
        kl_approximation_logging(all_states, pds, flat_grad, step, net, store)
        kl_vs_second_order_approx(all_states, pds, net, max_trpo_step, params, store, opt_step)

    # Backtracking line search
    with ch.no_grad():
        # Backtracking function, which gives the improvement on objective given an update direction s.
        def backtrack_fn(s):
            assign(initial_parameters + s.data, net.parameters())
            test_pds = net(all_states)
            test_action_log_probs = net.get_loglikelihood(test_pds, actions)
            new_reward = surrogate_reward(advs, new=test_action_log_probs, old=old_log_ps).mean()
            # surr_new is the surrogate before optimization.
            # We need to make sure the loss is improving, and KL between old probabilites are not too large.
            if params.TRPO_KL_REDUCE_FUNC == 'mean':
                kl_metric = net.calc_kl(pds, test_pds).mean()
            elif params.TRPO_KL_REDUCE_FUNC == 'max':
                kl_metric = net.calc_kl(pds, test_pds).max()
            else:
                raise ValueError("unknown reduce function " + params.TRPO_KL_REDUCE_FUNC)
            if new_reward <= surr_rew or kl_metric > params.MAX_KL:
                return -float('inf')
            return new_reward - surr_rew
        expected_improve = flat_grad @ max_trpo_step
        # max_trpo_step is the search direction. Backtracking line search will find a scaler for it.
        # expected_improve is the expected decrease in loss estimated by gradient.
        # backtracking_line_search will try a scaler 0.5, 0.25, 0.125, etc to achieve expected improvement.
        final_step = backtracking_line_search(backtrack_fn, max_trpo_step,
                                              expected_improve,
                                              num_tries=params.MAX_BACKTRACK)

        assign(initial_parameters + final_step, net.parameters())

    return surr_rew

def step_with_mode(mode):
    STEPS = {
        'trpo': trpo_step,
        'ppo': ppo_step,
        'robust_ppo': robust_ppo_step,
    }
    return STEPS[mode]


def get_params_norm(net, p=2):
    layer_norms = []
    layer_norms_dict = {}
    for name, params in net.named_parameters():
        if name != 'log_stdev' and name != 'log_weight' and params.ndim != 1:
            norm = ch.norm(params.view(-1), p=p).item() / np.prod(params.size())
            layer_norms.append(norm)
            layer_norms_dict[name] = norm
    return np.array(layer_norms), layer_norms_dict


last_norm = None

