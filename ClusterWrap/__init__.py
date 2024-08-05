from shutil import which
import os
from .clusters import slurm_cluster, local_cluster
from .clusters_lsf import janelia_lsf_cluster

cluster = local_cluster

if which('bsub') is not None:
    if os.system('bsub -V') != 32512:
        cluster = janelia_lsf_cluster
        
if which('srun') is not None:
    # Run 'srun --version' to verify it works correctly
    exit_status = os.system('srun --version')
    
    # Check if the command executed successfully
    if os.WEXITSTATUS(exit_status) == 0:
        cluster = slurm_cluster

