
import sys
import torch
from torch.utils import data
from torch.utils.data.sampler import SubsetRandomSampler
from torch.utils.tensorboard import SummaryWriter
from torch.cuda.amp import autocast,GradScaler
import torchvision as tv
import torch.nn as nn
import matplotlib.pyplot as plt
import subprocess
import numpy as np
from datetime import datetime
import argparse
import math
import zipfile

import utils.pytorch_shot_noise as pytorch_shot_noise
from nets.SLNet import SLNet
from utils.XLFMDataset import XLFMDatasetVol
from utils.misc_utils import *
from itertools import chain


# Arguments
parser = argparse.ArgumentParser()
parser.add_argument('--data_folder', nargs='?', default= "/u/home/vizcainj/share-all/XLFM-data/real_images/dataset_fish3_new/", help='Input training images path in format /XLFM_image/XLFM_image_stack.tif and XLFM_image_stack_S.tif in case of a sparse GT stack.')
parser.add_argument('--data_folder_test', nargs='?', default= "/u/home/vizcainj/share-all/XLFM-data/real_images/dataset_fish3_new/", help='Input testing image path')
parser.add_argument('--lenslet_file', nargs='?', default= "lenslet_centers_python.txt", help='Text file with the lenslet coordinates pairs x y "\n"')

parser.add_argument('--files_to_store', nargs='+', default=[], help='Relative paths of files to store in a zip when running this script, for backup.')
parser.add_argument('--prefix', nargs='?', default= "fishy", help='Prefix string for the output folder.')
parser.add_argument('--checkpoint', nargs='?', default= "", help='File path of checkpoint of previous run.')
# Images related arguments
parser.add_argument('--images_to_use', nargs='+', type=int, default=list(range(0,30,1)), help='Indeces of images to train on.')
parser.add_argument('--images_to_use_test', nargs='+', type=int, default=list(range(0,20,1)), help='Indeces of images to test on.')
parser.add_argument('--lenslet_crop_size', type=int, default=512, help='Side size of the microlens image.')
parser.add_argument('--img_size', type=int, default=2160, help='Side size of input image, square prefered.')
# Training arguments
parser.add_argument('--batch_size', type=int, default=8, help='Training batch size.') 
parser.add_argument('--learning_rate', type=float, default=0.0001, help='Training learning rate.')
parser.add_argument('--max_epochs', type=int, default=1001, help='Training epochs to run.')
parser.add_argument('--validation_split', type=float, default=0.1, help='Which part to use for validation 0 to 1.')
parser.add_argument('--eval_every', type=int, default=10, help='How often to evaluate the testing/validaton set.')
parser.add_argument('--shuffle_dataset', type=int, default=1, help='Radomize training images 0 or 1')
parser.add_argument('--use_bias', type=int, default=0, help='Use bias during training? 0 or 1')
parser.add_argument('--plot_images', type=int, default=0, help='Plot results with matplotlib?')
# Noise arguments
parser.add_argument('--add_noise', type=int, default=0, help='Apply noise to images? 0 or 1')
parser.add_argument('--signal_power_max', type=float, default=30**2, help='Max signal value to control signal to noise ratio when applyting noise.')
parser.add_argument('--signal_power_min', type=float, default=60**2, help='Min signal value to control signal to noise ratio when applyting noise.')
parser.add_argument('--norm_type', type=float, default=2, help='Normalization type, see the normalize_type function for more info.')
parser.add_argument('--dark_current', type=float, default=106, help='Dark current value of camera.')
parser.add_argument('--dark_current_sparse', type=float, default=0, help='Dark current value of camera.')
# Sparse decomposition arguments
parser.add_argument('--n_frames', type=int, default=3, help='Number of frames used as input to the SLNet.')
parser.add_argument('--rank', type=int, default=3, help='Rank enforcement for SVD. 6 is good')
parser.add_argument('--SL_alpha_l1', type=float, default=0.1, help='Threshold value for alpha in sparse decomposition.')
parser.add_argument('--SL_mu_sum_constraint', type=float, default=1e-2, help='Threshold value for mu in sparse decomposition.')
parser.add_argument('--weight_multiplier', type=float, default=0.5, help='Initialization multiplyier for weights, important parameter.')
# SLNet config
parser.add_argument('--temporal_shifts', nargs='+', type=int, default=[0,4,9], help='Which frames to use for training and testing.')
parser.add_argument('--use_random_shifts', nargs='+', type=int, default=0, help='Randomize the temporal shifts to use? 0 or 1')
parser.add_argument('--frame_to_grab', type=int, default=0, help='Which frame to show from the sparse decomposition?')
parser.add_argument('--l0_ths', type=float, default=0.05, help='Threshold value for alpha in nuclear decomposition')
# misc arguments
parser.add_argument('--output_path', nargs='?', default='experiments')
parser.add_argument('--main_gpu', nargs='+', type=int, default=[0], help='List of GPUs to use: [0,1]')
parser.add_argument('--slice_to_grab', nargs='+', type=int, default=60, help='slice to use for debug img')

n_threads = 0
args = parser.parse_args()
if len(args.main_gpu)>0:
    device = "cuda:" + str(args.main_gpu[0])
else:
    device = "cuda"
    args.main_gpu = [0]

if n_threads!=0:
    torch.set_num_threads(n_threads)

checkpoint_path = None
if len(args.checkpoint)>0:
    checkpoint = torch.load(args.checkpoint, map_location=device)
    checkpoint_path = args.checkpoint
    currArgs = args
    args = checkpoint['args']
    args.max_epochs = currArgs.max_epochs
    args.images_to_use = currArgs.images_to_use
    args.dark_current = currArgs.dark_current
    args.learning_rate = currArgs.learning_rate
    args.batch_size = currArgs.batch_size
    args.data_folder_test = currArgs.data_folder_test
    args.dark_current_sparse = currArgs.dark_current_sparse
args.shuffle_dataset = bool(args.shuffle_dataset)


# Get commit number 
label = subprocess.check_output(["git", "describe", "--always"]).strip()
save_folder = F"{args.output_path}/{datetime.now().strftime('%Y_%m_%d__%H:%M:%S')}__{args.main_gpu[0]}_gpu__{args.prefix}"

print(F'Logging directory: {save_folder}')

# Load datasets
args.subimage_shape = 2*[args.lenslet_crop_size]
args.output_shape = 2*[args.lenslet_crop_size]
dataset = XLFMDatasetVol(args.data_folder, args.lenslet_file, args.subimage_shape, img_shape=2*[args.img_size],
            images_to_use=args.images_to_use, load_vols=True, load_sparse=False, temporal_shifts=args.temporal_shifts, use_random_shifts=args.use_random_shifts)


#dataset_test = XLFMDatasetFull(args.data_folder_test, args.lenslet_file, args.subimage_shape, 2*[args.img_size],  
            # images_to_use=args.images_to_use_test, load_vols=True, load_sparse=False)

# Get normalization values 
max_images,max_images_sparse,max_volumes = dataset.get_max() 
mean_imgs,std_images,mean_vols,std_vols = dataset.get_statistics()

# Creating data indices for training and validation splits:
dataset_size = len(dataset)
indices = list(range(dataset_size))
split = int(np.ceil(args.validation_split * dataset_size))

torch.manual_seed(261290)

if args.shuffle_dataset :
    np.random.shuffle(indices)
train_indices, val_indices = indices[split:], indices[:split]
# Create dataloaders
train_sampler = SubsetRandomSampler([0])
valid_sampler = SubsetRandomSampler([0])

data_loaders = \
    {'train' : \
            data.DataLoader(dataset, batch_size=args.batch_size, 
                                sampler=train_sampler, pin_memory=False, num_workers=n_threads), \
    'val'   : \
            data.DataLoader(dataset, batch_size=args.batch_size,
                                    sampler=valid_sampler, pin_memory=False, num_workers=n_threads), \
    # 'test'  : \
            # data.DataLoader(dataset_test, batch_size=1, pin_memory=False, num_workers=n_threads, shuffle=True)
    }


# Weight initialization function
def init_weights(m):
    if type(m) == nn.Conv2d or type(m) == nn.Conv3d or type(m) == nn.ConvTranspose2d:
        torch.nn.init.kaiming_uniform_(m.weight,a=math.sqrt(2))
        m.weight.data = m.weight.data.abs()*args.weight_multiplier


# Create net
networks = []
net1 = SLNet(dataset.get_n_temporal_frames(), use_bias=args.use_bias, mu_sum_constraint=args.SL_mu_sum_constraint, alpha_l1=args.SL_alpha_l1).to(device)
net1.apply(init_weights)
net2 = SLNet(dataset.get_n_temporal_frames(), use_bias=args.use_bias, mu_sum_constraint=args.SL_mu_sum_constraint, alpha_l1=args.SL_alpha_l1).to(device)
net2.apply(init_weights)
net3 = SLNet(dataset.get_n_temporal_frames(), use_bias=args.use_bias, mu_sum_constraint=args.SL_mu_sum_constraint, alpha_l1=args.SL_alpha_l1).to(device)
net3.apply(init_weights)
net4 = SLNet(dataset.get_n_temporal_frames(), use_bias=args.use_bias, mu_sum_constraint=args.SL_mu_sum_constraint, alpha_l1=args.SL_alpha_l1).to(device)
net4.apply(init_weights)
networks.append(net1)
networks.append(net2)
networks.append(net3)
networks.append(net4)

# Use multiple gpus?
# if len(args.main_gpu)>1:
#     net = nn.DataParallel(net, args.main_gpu, args.main_gpu[0])
#     print("Let's use", torch.cuda.device_count(), "GPUs!")

# Trainable parameters
trainable_params = chain(net1.parameters(), net2.parameters(), net3.parameters(), net4.parameters())
params = sum([np.prod(p.size()) for p in net1.parameters()])*len(networks)

# Create optimizer
optimizer = torch.optim.Adam(trainable_params, lr=args.learning_rate)

# create gradient scaler for mixed precision training
scaler = GradScaler()

# Is there a checkpoint? load it
start_epoch = 0
if checkpoint_path:
    # net.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    scaler.load_state_dict(checkpoint['scaler_state_dict'])
    start_epoch = checkpoint['epoch']-1
    save_folder += '_C'

# Create summary writer to log stuff
writer = SummaryWriter(log_dir=save_folder)
writer.add_text('arguments',str(vars(args)),0)
writer.flush()
writer.add_scalar('params/', params)

# Store files for backup
zf = zipfile.ZipFile(save_folder + "/files.zip", "w")
for ff in args.files_to_store:
    zf.write(ff)
zf.close()

# timers
start = torch.cuda.Event(enable_timing=True)
end = torch.cuda.Event(enable_timing=True)

# Loop over epochs
for epoch in range(start_epoch, args.max_epochs):
    for curr_train_stage in ['train','val']:#,'test']:
        # Grab current data_loader
        curr_loader = data_loaders[curr_train_stage]
        curr_loader_len = curr_loader.sampler.num_samples if curr_train_stage=='test' else len(curr_loader.batch_sampler.sampler.indices)

        if curr_train_stage=='train':
            for net in networks:
                net.train()
            torch.set_grad_enabled(True)
        if curr_train_stage=='val' or curr_train_stage=='test':
            if epoch%args.eval_every!=0:
                continue
            for net in networks:
                net.eval()
            torch.set_grad_enabled(False)


        # Store losses of current epoch
        mean_losses = [0,0,0,0] 
        mean_sparse_l1 = [0,0,0,0]
        mean_psnr = 0
        mean_time = 0
        mean_eigen_values = torch.zeros([args.n_frames])
        mean_eigen_values_cropped = torch.zeros([args.n_frames])
        mean_eigen_crop = 0

        perf_metrics = {}
        perf_metrics['Rank_SLNet'] = []
        perf_metrics['Fro_SLNet'] = []
        perf_metrics['Fro_Ratio_SLNet'] = []
        perf_metrics['mean_error_SLNet'] = []
        perf_metrics['L1_SLNet'] = []

        # Training
        for ix,(_, curr_volume) in enumerate(curr_loader):
            for i in range(0,4):
                    
                #curr_slice = curr_volume[:,:,slice,...]
                # curr_volume = curr_volume[:,:,30*i:30*i+29,...]
                curr_slice = curr_volume[:,:,20*(i+1):20*(i+2),...].permute(0,2,1,3,4).reshape((-1,3,600,600))

                curr_slice = curr_slice.to(device)
                
                # if GT sparse images are not loaded, then let's replicate the input images to avoid errors
                if not curr_loader.dataset.load_sparse:
                    curr_slice = curr_slice.unsqueeze(-1).repeat(1,1,1,1,1,2)
                assert len(curr_slice.shape)>=6, "If sparse is used curr_slice should contain both images, dense and sparse stacked in the last dim."
                curr_slice_sparse = curr_slice[...,-1].clone().to(device).squeeze(0)
                curr_slice = curr_slice[...,0].squeeze(0)

                if True: # todo flag to check if it's a real dataset
                    curr_slice -= args.dark_current
                    curr_slice = F.relu(curr_slice).detach()
                    curr_slice_sparse -= args.dark_current_sparse
                    curr_slice_sparse = F.relu(curr_slice_sparse).detach()

                # Apply noise if needed, and only in the test set, as the train set comes from real images
                if args.add_noise==1 and curr_train_stage!='test':
                    curr_max = curr_slice.max()
                    # Update new signal power
                    signal_power = (args.signal_power_min + (args.signal_power_max-args.signal_power_min) * torch.rand(1)).item()
                    curr_slice = signal_power/curr_max * curr_slice
                    # Add noise
                    curr_slice = pytorch_shot_noise.add_camera_noise(curr_slice)
                    curr_slice = curr_slice.to(device)

                    
                # Normalize input images
                curr_slice, _ = normalize_type(curr_slice, 0, args.norm_type, mean_imgs, std_images, mean_vols, std_vols, max_images, max_volumes)
                
                if curr_train_stage=='train':
                    networks[i].zero_grad()
                    optimizer.zero_grad()
                
                with autocast():
                    #torch.cuda.synchronize()
                    #start.record()
                    # Predict dense part with the network

                    dense_part = networks[i](curr_slice)
                    # print(dense_part.shape)
                    dense_part = F.relu(dense_part)

                    # Compute sparse part
                    sparse_part = F.relu(curr_slice-dense_part)

                    # Measure time
                    #end.record()
                    #torch.cuda.synchronize()
                    #end_time = start.elapsed_time(end) / curr_slice.shape[0]
                    #mean_time += end_time

                    # Compute sparse decomposition on a patch, as the full image doesn't fit in memory due to SVD
                    # center = 64
                    # if curr_train_stage!='train':
                    #     center = 32
                    # coord_to_crop = torch.randint(center,dense_part.shape[3]-center, [2])
                    
                    # Grab patches
                    # dense_crop = dense_part[:,:,coord_to_crop[0]-center:coord_to_crop[0]+center,coord_to_crop[1]-center:coord_to_crop[1]+center].contiguous()
                    # sparse_crop = sparse_part[:,:,coord_to_crop[0]-center:coord_to_crop[0]+center,coord_to_crop[1]-center:coord_to_crop[1]+center].contiguous()
                    # curr_img_crop = curr_slice[:,:,coord_to_crop[0]-center:coord_to_crop[0]+center,coord_to_crop[1]-center:coord_to_crop[1]+center].detach()
                    
                    # Reconstruction error
                    Y = (curr_slice - dense_part - sparse_part)
                    # Nuclear norm
                    dense_vector = dense_part.view(dense_part.shape[0],dense_part.shape[1],-1)
                    with autocast(enabled=False):
                        print("doing svd")
                        (u,s,v) = torch.svd_lowrank(dense_vector.permute(0,2,1).float(), q=args.rank)
                        #print("done svd")
                        sOriginal = torch.autograd.Variable(s.clone())
                        # eigenvalues thresholding operation
                        s = torch.sign(s) * torch.max(s.abs() - networks[i].mu_sum_constraint, torch.zeros_like(s))

                    mean_eigen_values += sOriginal.mean(dim=0).detach().cpu()
                    mean_eigen_values_cropped += s.mean(dim=0).detach().cpu()
                    
                    # Reconstruct the images from the eigen information
                    for nB in range(s.shape[0]):
                        currS = torch.diag(s[nB,:])
                        dense_vector[nB,...] = torch.mm(torch.mm(u[nB,...], currS), v[nB,...].t()).t()
                    reconstructed_dense = dense_vector.view(dense_part.shape)

                    # Compute full loss
                    full_loss = F.l1_loss(reconstructed_dense,curr_slice) + networks[i].alpha_l1 * sparse_part.abs().mean() + Y.abs().mean()

                    sparse_part = F.relu(curr_slice - reconstructed_dense)
                    
                    #curr_volume[:,:,slice,...] = sparse_part

                    if ix==0 and args.plot_images and epoch%1 == 0 and curr_train_stage == 'train':
                        import matplotlib as mpl
                        mpl.use('Agg')
                        plt.clf()
                        # print("I am plotting")
                        plt.set_cmap('bwr')

                        for n in range(0,3):
                            plt.subplot(3,5,5*n+1)
                            plt.imshow(curr_slice[args.slice_to_grab,n,...].squeeze(0).detach().cpu().float().numpy())
                            plt.title('Input')
                            plt.subplot(3,5,5*n+2)
                            plt.imshow(dense_part[args.slice_to_grab,n,...].squeeze(0).detach().cpu().float().numpy())
                            plt.title('Dense prediction')
                            plt.subplot(3,5,5*n+3)
                            plt.imshow(sparse_part[args.slice_to_grab,n,...].squeeze(0).detach().cpu().float().numpy())
                            plt.title('Sparse prediction')
                            plt.subplot(3,5,5*n+4)
                            plt.imshow(Y[args.slice_to_grab,n,...].squeeze(0).detach().cpu().float().numpy())
                            plt.title('Y')
                            plt.subplot(3,5,5*n+5)
                            plt.imshow((dense_part - sparse_part)[args.slice_to_grab,n,...].squeeze(0).detach().cpu().float().numpy())
                            plt.title('(dense - sparse')
                        #plt.pause(0.1)       
                        plt.savefig("tmp.png")
                        print("finished plotting")


                    if curr_train_stage=='train':
                        full_loss.backward()

                        # Check fo NAN in training
                        broken = False
                        with torch.no_grad():
                            for param in networks[i].parameters():
                                if param.grad is not None:
                                    if torch.isnan(param.grad.mean()):
                                        broken = True
                        if broken:
                            continue

                        optimizer.step()


                    # detach tensors for display
                    
                    curr_slice_sparse = curr_slice_sparse.detach()
                    curr_slice = curr_slice.detach()
                    dense_part = dense_part.detach()
                    sparse_part = sparse_part.detach()

                    # Normalize back
                    curr_slice,_ = normalize_type(curr_slice.float(), 0, args.norm_type, mean_imgs, std_images, mean_vols, std_vols, max_images, max_volumes, inverse=True)
                    sparse_part,_ = normalize_type(sparse_part.float(), 0, args.norm_type, mean_imgs, std_images, mean_vols, std_vols, max_images, max_volumes, inverse=True)
                    dense_part,_ = normalize_type(dense_part.float(), 0, args.norm_type, mean_imgs, std_images, mean_vols, std_vols, max_images, max_volumes, inverse=True)
                    
                    sparse_part = F.relu(curr_slice-dense_part.detach())
                    mean_losses[i] += full_loss.item()
                    mean_sparse_l1[i] = F.relu(sparse_part).mean().item()
        # if ix == 1:
            # break
        # Compute different performance metrics
        for loss in mean_losses:
            loss /= curr_loader_len
        # mean_psnr = 20 * torch.log10(max_images / torch.sqrt(torch.tensor(mean_losses))) 
        mean_time /= curr_loader_len
        mean_eigen_values /= curr_loader_len
        mean_eigen_values_cropped /= curr_loader_len
        mean_eigen_crop = 0
        if mean_eigen_values.sum().item()!=0:
            mean_eigen_crop = mean_eigen_values_cropped.sum().item()/mean_eigen_values.sum().item()
        # mean_sparse_l1 = F.relu(sparse_part).mean().item()


        if epoch%args.eval_every==0:
            for i in range(0,4):
                print("eval")
                # Create debug images
                M = curr_slice[:,args.frame_to_grab,...].unsqueeze(1).to(device)
                S_SLNet = sparse_part[:,args.frame_to_grab,...].unsqueeze(1).to(device)
                L_SLNet = dense_part[:,args.frame_to_grab,...].unsqueeze(1).to(device)
                Rank_SLNet = torch.matrix_rank(L_SLNet[args.slice_to_grab,0,...].float()).item()

                fro_M = torch.norm(M).item()
                fro_SLNet = torch.norm(M-L_SLNet-S_SLNet).item()
                mean_error = (M-L_SLNet-S_SLNet).mean().item()
                L1_SLNet = (S_SLNet>(args.l0_ths*S_SLNet.max())).float().sum().item() / torch.numel(S_SLNet)

                perf_metrics['L1_SLNet'].append(L1_SLNet)
                perf_metrics['mean_error_SLNet'].append(mean_error)
                perf_metrics['Rank_SLNet'].append(Rank_SLNet)
                perf_metrics['Fro_SLNet'].append(fro_SLNet)
                perf_metrics['Fro_Ratio_SLNet'].append(fro_SLNet/fro_M)

                
                input_noisy_grid = tv.utils.make_grid(curr_slice[args.slice_to_grab,0,...].float().unsqueeze(0).cpu().data.detach(), normalize=True, scale_each=False)

                sparse_part = F.relu(sparse_part.detach()).float()
                dense_prediction = F.relu(dense_part.detach()).float()
                reconstructed_dense_prediciton = F.relu(reconstructed_dense.detach()).float()

                Y = sparse_part+dense_prediction

                sparse_part /= Y.max()
                input_intermediate_sparse_grid = tv.utils.make_grid(sparse_part[args.slice_to_grab,0,...].float().unsqueeze(0).cpu().data.detach(), normalize=True, scale_each=False)
                
                dense_prediction /= Y.max()
                input_intermediate_dense_grid = tv.utils.make_grid(dense_prediction[args.slice_to_grab,0,...].float().unsqueeze(0).cpu().data.detach(), normalize=True, scale_each=False)
                
                dense_prediction /= Y.max()
                input_intermediate_recon_dense_grid = tv.utils.make_grid(dense_prediction[args.slice_to_grab,0,...].float().unsqueeze(0).cpu().data.detach(), normalize=True, scale_each=False)
                
                input_intermediate_sparse_GT_grid = tv.utils.make_grid(curr_slice_sparse[args.slice_to_grab,0,...].float().unsqueeze(0).cpu().data.detach(), normalize=True, scale_each=False)
                
                writer.add_image('input_noisy_'+curr_train_stage, input_noisy_grid, epoch)
                writer.add_image('image_intermediate_sparse'+curr_train_stage, input_intermediate_sparse_grid, epoch)
                writer.add_image('image_intermediate_dense'+curr_train_stage, input_intermediate_dense_grid, epoch)
                writer.add_image('image_reconSVC_dense'+curr_train_stage, input_intermediate_recon_dense_grid, epoch)
                writer.add_image('GT_S_'+curr_train_stage, input_intermediate_sparse_GT_grid, epoch)
                writer.add_scalar('Loss/'+curr_train_stage+str(i), mean_losses[i], epoch)
                writer.add_scalar('Loss/mean_sparse_l1_'+curr_train_stage + str(i), mean_sparse_l1[i], epoch)
                # writer.add_scalar('regularization_weights/alpha_l1', net.alpha_l1, epoch)
                # writer.add_scalar('regularization_weights/mu_sum_constraint', net.mu_sum_constraint.item(), epoch)
                writer.add_scalar('regularization_weights/eigen_crop_percentage', mean_eigen_crop, epoch)
                # writer.add_scalar('psnr/'+curr_train_stage, mean_psnr, epoch)
                writer.add_scalar('times/'+curr_train_stage, mean_time, epoch)
                writer.add_scalar('lr/'+curr_train_stage, args.learning_rate, epoch)
                
                # writer.add_histogram('eigenvalues/'+curr_train_stage, mean_eigen_values, epoch)
                # writer.add_histogram('eigenvalues_cropped/'+curr_train_stage, mean_eigen_values_cropped, epoch)


                for k,v in perf_metrics.items():
                    writer.add_scalar('metrics/'+k+'_'+curr_train_stage, v[-1], epoch)

        print(str(epoch) + ' ' + curr_train_stage + " loss: " + str(mean_losses[0]) + " eigenCrop: " + str(mean_eigen_crop) + " time: " + str(mean_time))#, end="\r")

        if epoch%10==0:
            print("saving")
            torch.save({
            'epoch': epoch,
            'args' : args,
            'statistics' : [mean_imgs,std_images,mean_vols,std_vols ],
            # 'model_state_dict': net.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'scaler_state_dict' : scaler.state_dict(),
            'loss': mean_losses[0]},
            save_folder + '/model_'+str(epoch))
