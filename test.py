# %pylab inline
import numpy as np
import pandas as pd
import torch
import os
from torch import nn
from torch import optim
from torch.nn import functional as F
from torch import autograd
from einops import rearrange
from torch.autograd import Variable
import nibabel as nib
from tqdm.auto import tqdm
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader, Dataset
from skimage.metrics import structural_similarity as ssim
from numpy.lib.stride_tricks import as_strided
from nilearn import plotting
import matplotlib.pyplot as plt
from scipy.ndimage import rotate
from matplotlib import transforms
from Models import *


def pool3d(A, kernel_size, stride, padding=0, pool_mode='avg'):
    '''
    3D Pooling

    Parameters:
    A: input 3D array
    kernel_size: int, the size of the window over which we take pool
    stride: int, the stride of the window
    padding: int, implicit zero paddings on both sides of the input
    pool_mode: string, 'max' or 'avg'
    '''
    # Padding
    # A = np.pad(A, padding, mode='constant')
    # Window view of A
    output_shape = ((A.shape[0] - kernel_size)//stride + 1,
                    (A.shape[1] - kernel_size)//stride + 1,
                    (A.shape[2] - kernel_size)//stride + 1)
    shape_w = (output_shape[0], output_shape[1], output_shape[2], kernel_size, kernel_size, kernel_size)
    strides_w = (stride*A.strides[0], stride*A.strides[1], stride*A.strides[2], A.strides[0], A.strides[1], A.strides[2])
    A_w = as_strided(A, shape_w, strides_w)
    # Return the result of pooling
    # print('Pooled/n')
    if pool_mode == 'max':
        return A_w.max(axis=(3, 4, 5))
    elif pool_mode == 'avg':
        return A_w.mean(axis=(3, 4, 5))



def normz(x, normz_val=1, is_torch=True):
    if is_torch:
        return (x-torch.min(x))/(torch.max(x)-torch.min(x))*normz_val
    else: return (x-np.min(x))/(np.max(x)-np.min(x))*normz_val

def pad0(x, front=15, back=17):
    padsf = np.zeros((front, x.shape[1], x.shape[2]), dtype=np.float32)
    padsb = np.zeros((back, x.shape[1], x.shape[2]), dtype=np.float32)
    return np.concatenate((padsf, x, padsb), axis=0)


def pad_0cov(x, front=7, back=7, left=6, right=7):
    # Pad the 0th dimension
    padsf = np.zeros((front, x.shape[1], x.shape[2]), dtype=np.float32)
    padsb = np.zeros((back, x.shape[1], x.shape[2]), dtype=np.float32)
    x = np.concatenate((padsf, x, padsb), axis=0)

    # Pad the 2nd dimension
    padsl = np.zeros((x.shape[0], x.shape[1], left), dtype=np.float32)
    padsr = np.zeros((x.shape[0], x.shape[1], right), dtype=np.float32)
    x = np.concatenate((padsl, x, padsr), axis=2)

    return x


def add_gauss(example, prob:float=0.0):
    if prob>np.random.uniform(0,1):
        noise = torch.randn(example.size())
        noise = normz(noise).to(device)
        example = example + noise
    return example


class BrainDataset(Dataset):
    def __init__(self, annotations_file, transform=None, target_transform=None):
        self.annotations = annotations_file #load csv file containing training examples using pandas
        self.transform = transform
        self.target_transform = target_transform

    def __len__(self):
        return len(self.annotations)

    def __getitem__(self, idx):
        example_path, dataset_name = os.path.abspath(self.annotations.iloc[idx, 0]), self.annotations.iloc[idx,1]
        # load example into numpy array using nibabel
        example = nib.load(example_path)
        if dataset_name=='AOMIC':
            example = np.array(example.dataobj, dtype=np.float32)
            example = example[:, 23:215, 10:202]
            example = pad0(example)
            example = pool3d(example, 3, 3)
        elif dataset_name=='ADNI':
            example = np.array(example.get_fdata(), dtype=np.float32)
            example = pool3d(example[34:226,29:221,19:211],3,3)
            example = np.swapaxes(example, 1,2)
            example = np.flip(example, axis=2)
            # print('got_item, ADNI\n')
        elif dataset_name=='ABIDE':
            example = np.array(example.get_fdata(), dtype=np.float32)
            example = pad0(example, 1, 2)
        elif dataset_name=='COVID':
            if example.shape[0]==256:
                example = example[26:218, 26:218, :]
            else:
                example = example[:, 26:218, 5:160]
            example = pool3d(example, 3, 3)
            if example.shape[0]==49:
                example = pad_0cov(example, back=8)
            elif example.shape[0]==64:
                example = pad_0cov(example, front=0, back=0, left=7, right=8)
            else:
                example = pad_0cov(example)
        example = normz(example, is_torch=False)
        example = torch.from_numpy(np.expand_dims(example, axis=0)) # , dtype=torch.float32
        example = example.to(device)
        label = example.clone()
        if self.transform:
            example = self.transform(example)
        if self.target_transform:
            label = self.target_transform(label)
        return example, label
    



def inf_train_gen(data_loader):
    while True:
        for _,images in enumerate(data_loader):
            yield images
            




def calc_gradient_penalty(model, x, x_gen, w=10):
    """WGAN-GP gradient penalty"""
    assert x.size()==x_gen.size(), "real and sampled sizes do not match"
    alpha_size = tuple((len(x), *(1,)*(x.dim()-1)))
    alpha = torch.FloatTensor(*alpha_size).uniform_().to(device)

    x_hat = x.data*alpha + x_gen.data*(1-alpha)
    x_hat = Variable(x_hat, requires_grad=True)

    def eps_norm(x):
        x = x.view(len(x), -1)
        return (x*x+_eps).sum(-1).sqrt()
    def bi_penalty(x):
        return (x-1)**2

    grad_xhat = torch.autograd.grad(model(x_hat).sum(), x_hat, create_graph=True, only_inputs=True)[0]

    penalty = w*bi_penalty(eps_norm(grad_xhat)).mean()
    return penalty



###################
##### METRICS #####
###################


def MMD(x, y, kernel):
    """Emprical maximum mean discrepancy. The lower the result
       the more evidence that distributions are the same.

    Args:
        x: first sample, distribution P
        y: second sample, distribution Q
        kernel: kernel type such as "multiscale" or "rbf"
    """
    xx, yy, zz = torch.mm(x, x.t()), torch.mm(y, y.t()), torch.mm(x, y.t())
    rx = (xx.diag().unsqueeze(0).expand_as(xx))
    ry = (yy.diag().unsqueeze(0).expand_as(yy))

    dxx = rx.t() + rx - 2. * xx # Used for A in (1)
    dyy = ry.t() + ry - 2. * yy # Used for B in (1)
    dxy = rx.t() + ry - 2. * zz # Used for C in (1)

    XX, YY, XY = (torch.zeros(xx.shape).to(device),
                  torch.zeros(xx.shape).to(device),
                  torch.zeros(xx.shape).to(device))

    if kernel == "multiscale":

        bandwidth_range = [0.2, 0.5, 0.9, 1.3]
        for a in bandwidth_range:
            XX += a**2 * (a**2 + dxx)**-1
            YY += a**2 * (a**2 + dyy)**-1
            XY += a**2 * (a**2 + dxy)**-1

    if kernel == "rbf":

        bandwidth_range = [10, 15, 20, 50]
        for a in bandwidth_range:
            XX += torch.exp(-0.5*dxx/a)
            YY += torch.exp(-0.5*dyy/a)
            XY += torch.exp(-0.5*dxy/a)

    return torch.mean(XX + YY - 2. * XY)

def mmd_eval(img, gen):
    img = torch.round(normz(img, 255))
    gen = torch.round(normz(gen, 255))
    img = img.view(img.size(0), img.size(2) * img.size(3)*img.size(4))
    gen = gen.view(gen.size(0), gen.size(2) * gen.size(3)*gen.size(4))
    mmd_score = MMD(img, gen, 'multiscale')
    return mmd_score


def ssim_eval(img, gen):
    img = torch.round(normz(img, 255))
    gen = torch.round(normz(gen, 255))
    img = img.squeeze().cpu().detach().numpy()
    gen = gen.squeeze().cpu().detach().numpy()
    window_size = min(img.shape[0]//2*2-1, img.shape[1]//2*2-1, img.shape[2]//2*2-1, 7)
    # ssim_noise = ssim(img.squeeze().cpu().numpy(), gen.squeeze().cpu().numpy(), data_range=255)    #img_noise.max() - img_noise.min())
    ssim_noise = ssim(img, gen, data_range=255, win_size=window_size)    #img_noise.max() - img_noise.min())
    # print('on ssim\n')
    return ssim_noise



####################
####### TEST #######
####################

def test(exp_no:int, pretrained:bool, TOTAL_ITER:int=200000, viz=True):
    checkpoint_dir = f'./checkpoint{exp_no}'
    logfile = f'{checkpoint_dir}/test{exp_no}.txt'
    if not pretrained:
        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)
        print(f'This test results are for Experiment: {exp_no} with batch_size: {BATCH_SIZE} and the following parameters (learning rates): \nelr: {elr} \t\t glr: {glr} \t\t dlr: {dlr} \t\t cdlr: {cdlr} \t\t g_iter: {g_iter} \t\t cd_iter: {cd_iter} \t\t gaussian_noise: {noise_prob} \n')
        state = open(logfile, 'w')
        state.write(f'This test results are for Experiment: {exp_no} with Attention, batch_size: {BATCH_SIZE}, from Scratch and the following parameters (learning rates): \nelr: {elr} \t\t glr: {glr} \t\t dlr: {dlr} \t\t cdlr: {cdlr} \t\t g_iter: {g_iter} \t\t cd_iter: {cd_iter} \t\t gaussian_noise: {0.0} on ADNI \n')
        state.close()
    else:
        print(f'Resuming Experiment: {exp_no}, from {cheqpiter} iterations. May train upto 12k iterations as directed in the paper, and importantly,  ~ {len(train_loader)/(d_iter+g_iter+cd_iter):<8.5} iterations will complete {1} epoch of training. \n')


    with tqdm(range(len(train_loader)+1), ncols=70) as tkdm:
        tkdm.set_description_str('iterations')
    
        E.eval()
        G.eval()
        D.eval()
        # A.eval()

        ssim_avgs = 0.0
        
        for iteration in tkdm:
            for p in D.parameters():  
                p.requires_grad = False
            for p in E.parameters():  
                p.requires_grad = False
            for p in G.parameters():  
                p.requires_grad = False
            # for p in A.parameters():  
            #     p.requires_grad = False
            
            ###############################################
            # Visualization
            ###############################################
            
            real_images, _ = gen_load.__next__()
            real_images = (Variable(real_images, requires_grad=False).type(torch.cuda.FloatTensor)).cuda(device, non_blocking=True)
            _batch_size = real_images.size(0)

            z_hat = E(real_images)
            z_hat = z_hat.view(_batch_size,-1)
            # print('\n', z_hat.shape, 'pre rearranging\n')
            # z_hat = rearrange(z_hat, 'b (k n o p)-> b n (k o p)', n=1,o=1,p=1)
            # # print(z_hat.shape, 'post rearranging\n')
            # z_hat = A(normz(z_hat))
            # z_hat = E(real_images).view(_batch_size,-1)
            x_hat = normz(G(z_hat))

            
            if cd_iter:
                z_rand = (Variable(torch.randn((_batch_size,latent_dim)), requires_grad=False).type(torch.cuda.FloatTensor)).cuda(device, non_blocking=True)
                x_rand = normz(G(z_rand))



            state = open(logfile, 'a')
            ssim_scrore =  ssim_eval(x_hat,real_images)
            ssim_avgs+=ssim_scrore
            state.write(f'[{iteration+cheqpiter}/{TOTAL_ITER[1]}] \t\t ssim( gen: {ssim_scrore:<8.5} \t mmd( gen: {mmd_eval(x_hat,real_images)}))\n')     # , random: {ssim_eval(x_rand, real_images):<8.5     # , random: {mmd_eval(x_rand, real_images)}
            state.close()

            if iteration % 5 == 0 and viz:
                fig, axs = plt.subplots(2, 3)
                plot_axs = list(range(28,35))
                indxs = [np.random.choice(plot_axs), np.random.choice(plot_axs), np.random.choice(plot_axs)]
                angle = 90
                tr = transforms.Affine2D().rotate_deg(angle)

                # Plot the first image
                feat = np.squeeze((0.5*real_images[0]+0.5).data.cpu().numpy())
                feat = nib.Nifti1Image(feat,affine = np.eye(4))
                axs[0,0].get_xaxis().set_visible(False)
                axs[0,0].get_yaxis().set_visible(False)
                axs[0,1].get_xaxis().set_visible(False)
                axs[0,1].get_yaxis().set_visible(False)
                axs[0,2].get_xaxis().set_visible(False)
                axs[0,2].get_yaxis().set_visible(False)
                axs[0, 0].imshow(rotate(feat.get_fdata()[indxs[0],:,:], angle), cmap='gray')
                axs[0, 1].imshow(rotate(feat.get_fdata()[:,indxs[1],:], angle), cmap='gray')
                axs[0, 2].imshow(rotate(feat.get_fdata()[:,:,indxs[2]], angle), cmap='gray')
                # axs[0].imshow(np.asanyarray(feat.dataobj), cmap='gray')
                axs[0, 1].set_title(f"X_Real {indxs[1]}")
                axs[0, 0].set_title(indxs[0])
                axs[0, 2].set_title(indxs[2])

                # Plot the second image
                feat = np.squeeze((0.5*x_hat[0]+0.5).data.cpu().numpy())
                feat = nib.Nifti1Image(feat,affine = np.eye(4))
                # axs[1].imshow(feat.get_fdata(), cmap='gray')
                axs[1,0].get_xaxis().set_visible(False)
                axs[1,0].get_yaxis().set_visible(False)
                axs[1,1].get_xaxis().set_visible(False)
                axs[1,1].get_yaxis().set_visible(False)
                axs[1,2].get_xaxis().set_visible(False)
                axs[1,2].get_yaxis().set_visible(False)
                axs[1, 0].imshow(rotate(feat.get_fdata()[indxs[0],:,:], angle), cmap='gray')
                axs[1, 1].imshow(rotate(feat.get_fdata()[:,indxs[1],:], angle), cmap='gray')
                axs[1, 2].imshow(rotate(feat.get_fdata()[:,:,indxs[2]], angle), cmap='gray')
                axs[1, 1].set_title(f"X_DEC {indxs[1]}")
                axs[1, 0].set_title(indxs[0])
                axs[1, 2].set_title(indxs[2])


                # Save the figure with both images
                plt.savefig(f'{checkpoint_dir}/test{iteration+cheqpiter}.png', dpi=400)
                plt.close()
        state = open(logfile, 'a')
        state.write(f'Averages to: {ssim_avgs/(len(train_loader)+1):<8.5} \n\n\n')
        state.close()





######################
##### PARAMETERS #####
######################
exp_no = 34 
noise_prob = 0.0
device = 'cuda:0' if torch.cuda.is_available else 'cpu'
 
BATCH_SIZE=16
latent_dim = 1000   #setting latent variable sizes
gpu = True
workers = 4
LAMBDA= 10
_eps = 1e-15
Use_BRATS=False
Use_ATLAS = False

print("Selected device:", device, 'for exp_no', exp_no, 'with batch_size:', BATCH_SIZE)

## define DISCRIMINATOR IN THE init_model()
glr = 2e-4
dlr = 2e-4
elr = 2e-4
cdlr = 2e-4
g_iter = 2
d_iter = 1
cd_iter = 0
pretrained = True
cheqpiter = 501 if pretrained else 13000  # please define this as required


def load_models(exp_no, cheqpiter, CD_iter):
    G.load_state_dict(torch.load(f'./checkpoint{exp_no}/G_iter{cheqpiter}es.pth'))
    # D.load_state_dict(torch.load(f'./checkpoint{exp_no}/D_iter{cheqpiter}es.pth'))
    E.load_state_dict(torch.load(f'./checkpoint{exp_no}/E_iter{cheqpiter}es.pth'))
    # A.load_state_dict(torch.load(f'./checkpoint{exp_no}/A_iter{cheqpiter}es.pth'))
    

G = Generator(noise = latent_dim)
D = PatchGANdiscriminator()
E = Discriminator(out_class = latent_dim,is_dis=False)
# A = MultiHeadAttention()
# A = AttentionM()

load_models(exp_no, cheqpiter, cd_iter)

G.to(device)
D.to(device)
E.to(device)
# A.to(device)

g_optimizer = optim.Adam(G.parameters(), glr)
d_optimizer = optim.Adam(D.parameters(), dlr)
e_optimizer = optim.Adam(E.parameters(), elr)


criterion_bce = nn.BCELoss()
criterion_l1 = nn.L1Loss()
criterion_mse = nn.MSELoss()

    
test_files = pd.read_csv('/home/guest1/data/3drepr/test_AOMIC.csv', header=None)
testset = BrainDataset(test_files)
train_loader = DataLoader(testset, BATCH_SIZE, shuffle=True)
gen_load = inf_train_gen(train_loader)



test(exp_no, pretrained)


cheqpiters = [1000, 2000, 2501, 3000, 4000, 5000, 5501, 6000, 6501, 7000, 8000, 8501, 9000, 9501, 10000, 10501, 11000, 11501, 12000]
for i in cheqpiters:
    cheqpiter = i
    load_models(exp_no, cheqpiter, cd_iter)
    test(exp_no, pretrained)

