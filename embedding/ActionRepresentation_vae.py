# TODO: s discrete continue ->s"
import numpy as np
import torch
from torch import float32
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
from embedding.Utils.utils import NeuralNet, pairwise_distances, pairwise_hyp_distances, squash, atanh
from embedding.Utils import Basis
import torch.optim as optim

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
import torch.nn.functional as functional

# Vanilla Variational Auto-Encoder
class VAE(nn.Module):
    def __init__(self, state_dim, action_dim, action_embedding_dim, parameter_action_dim, latent_dim, max_action,
                 hidden_size=128):
        super(VAE, self).__init__()

        # embedding table
        init_tensor = torch.rand(action_dim,
                                 action_embedding_dim) * 2 - 1  # Don't initialize near the extremes.
        self.embeddings = torch.nn.Parameter(init_tensor.type(float32), requires_grad=True)

        self.e0_0 = nn.Linear(state_dim + action_embedding_dim, 2 * hidden_size)
        self.e0_1 = nn.Linear(parameter_action_dim, 2 * hidden_size)
        self.e1 = nn.Linear(2 * hidden_size, hidden_size)
        self.e2 = nn.Linear(2 * hidden_size, hidden_size)

        self.mean = nn.Linear(hidden_size, latent_dim)
        self.log_std = nn.Linear(hidden_size, latent_dim)

        self.d0_0 = nn.Linear(state_dim + action_embedding_dim, 2 * hidden_size)
        self.d0_1 = nn.Linear(latent_dim, 2 * hidden_size)
        self.d1 = nn.Linear(2 * hidden_size, hidden_size)
        self.d2 = nn.Linear(2 * hidden_size, hidden_size)

        self.parameter_action_output = nn.Linear(hidden_size, parameter_action_dim)
        self.d3 = nn.Linear(hidden_size, hidden_size)
        self.delta_state_output = nn.Linear(hidden_size, state_dim)

        # self.max_action = max_action
        self.latent_dim = latent_dim

    def forward(self, state, action, action_parameter):

        z, mean, std = self.encode(state, action, action_parameter)
        u, s = self.decode(state, z, action)

        return u, s, mean, std
    
    def encode(self, state, action, action_parameter):

        z_0 = F.relu(self.e0_0(torch.cat([state, action], 1)))
        z_1 = F.relu(self.e0_1(action_parameter))
        z = z_0 * z_1

        z = F.relu(self.e1(z))
        z = F.relu(self.e2(z))

        mean = self.mean(z)
        # Clamped for numerical stability
        log_std = self.log_std(z).clamp(-4, 15)
        std = torch.exp(log_std)

        z = mean + std * torch.randn_like(std)

        return z, mean, std
        

    def decode(self, state, z=None, action=None, clip=None):
        # When sampling from the VAE, the latent vector is clipped to [-0.5, 0.5]
        if z is None:
            z = torch.randn((state.shape[0], self.latent_dim)).to(device)
            if clip is not None:
                z = z.clamp(-clip, clip)
        v_0 = F.relu(self.d0_0(torch.cat([state, action], 1)))
        v_1 = F.relu(self.d0_1(z))
        v = v_0 * v_1
        v = F.relu(self.d1(v))
        v = F.relu(self.d2(v))

        parameter_action = self.parameter_action_output(v)

        # Cascade head to produce the prediction of the state residual of transition dynamics.
        v = F.relu(self.d3(v))
        state_residual = self.delta_state_output(v)

        # Latent Space Constraint (LSC)
        # In specific, we re-scale each dimension of the output of latent policy by tanh activation) to a bounded range [blower, bupper]. 
        return torch.tanh(parameter_action), torch.tanh(state_residual)


class Action_representation(NeuralNet):
    def __init__(self,
                 state_dim,
                 action_dim,
                 parameter_action_dim,
                 action_embedding_dim,
                 parameter_action_embedding_dim,
                 embed_lr=1e-4,
                 ):
        super(Action_representation, self).__init__()

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.state_dim = state_dim
        self.action_dim = action_dim
        self.parameter_action_dim = parameter_action_dim
        self.action_ebedding_dim = action_embedding_dim
        self.reduced_parameter_action_dim = parameter_action_embedding_dim
        self.embed_lr = embed_lr

        self.vae = VAE(state_dim=self.state_dim, action_dim=self.action_dim, action_embedding_dim=self.action_ebedding_dim, 
                       parameter_action_dim=self.parameter_action_dim, latent_dim=self.reduced_parameter_action_dim, max_action=1.0,
                       hidden_size=128).to(self.device) #TODO max_action
        
        self.vae_optimizer = torch.optim.Adam(self.vae.parameters(), lr=1e-4)

    def retrieve_embedding(self):

        return self.vae.embeddings

    def cal_loss(self, state, action, parameter_action, state_residual):

        pred_param_action, pred_state_residual, mean, std = self.vae(state, action, parameter_action)

        recon_loss = F.mse_loss(pred_param_action, parameter_action, size_average=True) 
        state_residual_loss = F.mse_loss(pred_state_residual, state_residual, size_average=True)
        

        KL_loss = -0.5 * (1 + torch.log(std.pow(2)) - mean.pow(2) - std.pow(2)).mean()

        # vae_loss = 0.25 * recon_loss_s + recon_loss_c + 0.5 * KL_loss
        # vae_loss = 0.25 * recon_loss_s + 2.0 * recon_loss_c + 0.5 * KL_loss  #best
        vae_loss = state_residual_loss + 2.0 * recon_loss + 0.5 * KL_loss
        # print("vae loss",vae_loss)
        # return vae_loss, 0.25 * recon_loss_s, recon_loss_c, 0.5 * KL_loss
        # return vae_loss, 0.25 * recon_loss_s, 2.0 * recon_loss_c, 0.5 * KL_loss #best
        return vae_loss, state_residual_loss, 2.0 * recon_loss, 0.5 * KL_loss

    def step(self, state, action, parameter_action, next_state, embed_lr=1e-4):
        
        state_batch = state.to(self.device)
        action_batch = self.get_embedding(action).to(self.device)
        parameter_action_batch = parameter_action.to(self.device)
        next_state_batch = next_state.to(self.device)


        vae_loss, state_residual_loss, recon_loss, KL_loss = self.cal_loss(state_batch, action_batch, parameter_action_batch, next_state_batch)

        self.vae_optimizer = torch.optim.Adam(self.vae.parameters(), lr=embed_lr)
        self.vae_optimizer.zero_grad()
        vae_loss.backward()
        self.vae_optimizer.step()

        return vae_loss.cpu().data.numpy(), state_residual_loss.cpu().data.numpy(), recon_loss.cpu().data.numpy(), KL_loss.cpu().data.numpy()

    def select_parameter_action(self, state, z, action):
        with torch.no_grad():
            state = torch.FloatTensor(state.reshape(1, -1)).to(self.device)
            z = torch.FloatTensor(z.reshape(1, -1)).to(self.device)
            action = torch.FloatTensor(action.reshape(1, -1)).to(self.device)
            parameter_action, state_residual = self.vae.decode(state, z, action)
        return parameter_action.cpu().data.numpy().flatten(), state_residual.cpu().data.numpy()

    # def select_delta_state(self, state, z, action):
    #     with torch.no_grad():
    #         action_c, state = self.vae.decode(state, z, action)
    #     return state.cpu().data.numpy() 

    def get_embedding(self, action):
        # Get the corresponding target embedding
        action_emb = self.vae.embeddings[action]
        action_emb = torch.tanh(action_emb)
        return action_emb

    def get_match_scores(self, action):
        # compute similarity probability based on L2 norm
        embeddings = torch.tanh(self.retrieve_embedding())
        action = action.to(self.device)
        # compute similarity probability based on L2 norm
        similarity = - pairwise_distances(action, embeddings)  # Negate euclidean to convert diff into similarity score
        return similarity

        # 获得最优动作，输出于embedding最相近的action 作为最优动作.

    def select_discrete_action(self, action):
        similarity = self.get_match_scores(action)
        val, pos = torch.max(similarity, dim=1)

        if len(pos) == 1:
            return pos.cpu().item()  # data.numpy()[0]
        else:
            return pos.cpu().numpy()

    # def retrieve_scale_offset(self, state, action, parameter_action, c_percent_rate=5):
    #     """
    #     Latent Space Constraint (LSC)
    #     Retrieve the scale and offset for each dimension of the latent space.
        
    #     Different with papar, it seems it only impose the constraint on the latent parameter action.
    #     Moreover, the the latent parameter action is not normalized to [-1, 1] by tanh activation.
    #     """
        
    #     state_batch = state.to(self.device)
    #     action_batch = self.get_embedding(action).to(self.device)
    #     parameter_action_batch = parameter_action.to(self.device)

    #     z, _, _ = self.vae.encode(state_batch, action_batch, parameter_action_batch)
    #     z = z.cpu().data.numpy()

    #     c_percent_borders = self.retrieve_c_percent_borders(z, c_percent_rate)

    #     scales = []
    #     offsets = []

    #     # Calculate scales and offsets based on c_percent_borders
    #     for dim in range(len(c_percent_borders)):
    #         scale = (c_percent_borders[dim][0] - c_percent_borders[dim][1]) / 2.0
    #         offset = (c_percent_borders[dim][0] + c_percent_borders[dim][1]) / 2.0
    #         scales.append(scale)
    #         offsets.append(offset)

    #     return scales, offsets

    # def retrieve_c_percent_borders(self, z, c_percent_rate=4):
    #     batch_size = z.shape[0] # Assuming the shape of z is [batch_size, num_dim]
    #     num_dim = z.shape[1]  
    #     border_idx = int(c_percent_rate / 100 * batch_size)

    #     # Initialize lists to store values for each dimension
    #     z_values = [[] for _ in range(num_dim)]
    #     c_percent_borders = [[] for _ in range(num_dim)] # c_percent_borders: [[c_up, c_down], [c_up, c_down], ...]

    #     # Iterate over each sample
    #     for i in range(len(z)):
    #         for dim in range(num_dim):
    #             z_values[dim].append(z[i][dim])

    #     # Sort the data for each dimension and calculate c_percent_borders
    #     for dim in range(num_dim):
    #         z_values[dim].sort()
    #         c_percent_up = z_values[dim][-border_idx - 1]
    #         c_percent_down = z_values[dim][border_idx]
    #         c_percent_borders[dim].extend([c_percent_up, c_percent_down])

    #     return c_percent_borders
    
    def save(self, filename, directory):
        torch.save(self.vae.state_dict(), '%s/%s_vae.pth' % (directory, filename))
        # torch.save(self.vae.embeddings, '%s/%s_embeddings.pth' % (directory, filename))

    def load(self, filename, directory):
        self.vae.load_state_dict(torch.load('%s/%s_vae.pth' % (directory, filename), map_location=self.device))
        # self.vae.embeddings = torch.load('%s/%s_embeddings.pth' % (directory, filename), map_location=self.device)