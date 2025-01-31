from dask.distributed import Client, LocalCluster
from dask_jobqueue import SLURMCluster
from dask_jobqueue.slurm import SLURMJob
import dask.config
from pathlib import Path
import os
import sys
import time
import yaml

class custom_SLURMJob(SLURMJob):
    cancel_command = "scancel"

class custom_SLURMCluster(SLURMCluster):
    job_cls = custom_SLURMJob

class _cluster(object):

    def __init__(self):
        self.client = None
        self.cluster = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self.persist_yaml:
            if os.path.exists(self.yaml_path):
                os.remove(self.yaml_path)
        self.client.close()
        self.cluster.__exit__(exc_type, exc_value, traceback)

    def set_client(self, client):
        self.client = client

    def set_cluster(self, cluster):
        self.cluster = cluster

    def modify_dask_config(self, options, yaml_name='ClusterWrap.yaml', persist_yaml=False):
        dask.config.set(options)
        yaml_path = str(Path.home()) + '/.config/dask/' + yaml_name
        with open(yaml_path, 'w') as f:
            yaml.dump(dask.config.config, f, default_flow_style=False)
        self.yaml_path = yaml_path
        self.persist_yaml = persist_yaml

    def get_dashboard(self):
        if self.cluster is not None:
            return self.cluster.dashboard_link

class slurm_cluster(_cluster):
    """
    A dask cluster configured to run on a SLURM compute cluster

    Parameters
    ----------
    ncpus : int (default: 4)
        The number of cpus that each SLURM worker should have.
        Of course this also controls total RAM: 15GB per cpu.
        See `processes` parameter for distinction between SLURM worker
        and dask worker.

    processes : int (default: 1)
        The number of dask workers that should spawn on each SLURM worker.
        A SLURM worker is a set of compute resources allocated by SLURM
        running some number of python processes. Each such python process
        is a dask worker. By increasing this parameter you increase the
        number of dask workers running within a single SLURM worker.

    threads : int (default: None)
        The number of compute threads dask is allowed to spawn on each
        dask worker. Often if you are using numpy or other highly
        multithreaded code, you do not want dask itself to spawn
        many additional compute threads. In that case, dask should have
        one or two compute threads per worker and any additional cpu cores
        on the worker will be utilized by the underlying multithreaded libraries.

    min_workers : int (default: 1)
        The minimum number of dask workers the cluster will have at any
        given time.

    max_workers : int (default 4)
        The maximum number of dask workers the cluster will have at any
        given time. The cluster will dynamically adjust the number of
        workers between `min_workers` and `max_workers` based on the
        number of tasks submitted by the scheduler.

    walltime : string (default: "3:59")
        The maximum lifetime of each SLURM worker (which comprises one
        or more dask workers). The default is chosen so that SLURM jobs
        will be submitted to the cloud queue by default, and could thus
        be run on any available cluster hardware.

    config : dict (default: {})
        Any additional arguments to configure dask, the distributed
        scheduler, the nanny etc. See:
        https://docs.dask.org/en/latest/configuration.html

    **kwargs : any additional keyword argument
        Additional arguments passed to dask_jobqueue.SLURMCluster.
        Notable arguments include:
        death_timeout, project, queue, env_extra
        See:
        https://jobqueue.dask.org/en/latest/generated/dask_jobqueue.SLURMCluster.html
        for a more complete list.

    Returns
    -------
    A configured slurm_cluster object ready to receive tasks
    from a dask scheduler
    """

    def __init__(
        self,
        ncpus=4,
        processes=1,
        threads=None,
        min_workers=1,
        max_workers=4,
        walltime="3:59",
        config={},
        **kwargs
    ):

        # Call super constructor
        super().__init__()

        # Set config defaults
        USER = os.environ["USER"]
        config_defaults = {
            'temporary-directory': f"/local/workdir/temp/{USER}/",
            'distributed.comm.timeouts.connect': '180s',
            'distributed.comm.timeouts.tcp': '360s',
        }
        config_defaults = {**config_defaults, **config}
        self.modify_dask_config(config_defaults)

        # Store ncpus/per worker and worker limits
        self.adapt = None
        self.ncpus = ncpus
        self.min_workers = min_workers
        self.max_workers = max_workers

        # Set environment vars
        tpw = 2 * ncpus  # threads per worker
        job_script_prologue = [
            f"export MKL_NUM_THREADS={tpw}",
            f"export NUM_MKL_THREADS={tpw}",
            f"export OPENBLAS_NUM_THREADS={tpw}",
            f"export OPENMP_NUM_THREADS={tpw}",
            f"export OMP_NUM_THREADS={tpw}",
        ]

        # Set local and log directories
        CWD = os.getcwd()
        PID = os.getpid()
        if "local_directory" not in kwargs:
            kwargs["local_directory"] = f"{CWD}/dask_workers/"
        if "log_directory" not in kwargs:
            log_dir = f"{CWD}/dask_worker_logs_{PID}/"
            Path(log_dir).mkdir(parents=False, exist_ok=True)
            kwargs["log_directory"] = log_dir

        # Compute ncpus/RAM relationship
        memory = str(15 * ncpus) + 'GB'

        # Determine nthreads
        if threads is None:
            threads = ncpus

        # Create cluster
        print('memory requested:', memory)
        cluster = custom_SLURMCluster(
            cores=threads,
            processes=processes,
            memory=memory,
            walltime=walltime,
            job_script_prologue=job_script_prologue,
            **kwargs,
        )

        # Connect cluster to client
        client = Client(cluster)
        self.set_cluster(cluster)
        self.set_client(client)
        print("Cluster dashboard link: ", cluster.dashboard_link)
        sys.stdout.flush()

        # Set adaptive cluster bounds
        self.adapt_cluster(min_workers, max_workers)

    def __exit__(self, exc_type, exc_value, traceback):
        super().__exit__(exc_type, exc_value, traceback)

    def change_worker_attributes(self, min_workers, max_workers, **kwargs):
        self.cluster.scale(0)
        for k, v in kwargs.items():
            self.cluster.new_spec['options'][k] = v
        self.adapt_cluster(min_workers, max_workers)

    def adapt_cluster(self, min_workers=None, max_workers=None):
        if min_workers is not None:
            self.min_workers = min_workers
        if max_workers is not None:
            self.max_workers = max_workers
        self.adapt = self.cluster.adapt(
            minimum_jobs=self.min_workers,
            maximum_jobs=self.max_workers,
            interval='10s',
            wait_count=6,
        )

        # Give feedback to user
        mn, mx, nc = self.min_workers, self.max_workers, self.ncpus  # shorthand
        print(f"Cluster adapting between {mn} and {mx} workers with {nc} cores per worker")

class local_cluster(_cluster):
    """
    This is a thin wrapper around dask.distributed.LocalCluster
    For a list of full arguments (how to specify your worker resources)
    see:
    https://distributed.dask.org/en/latest/api.html#distributed.LocalCluster

    You need to know how many cpu cores and how much RAM your machine has.
    Most users will only need to specify:
    n_workers
    memory_limit (which is the limit per worker)
    threads_per_workers (for most workflows this should be 1)
    """

    def __init__(
        self,
        config={},
        memory_limit=None,
        **kwargs,
    ):

        # Initialize base class
        super().__init__()

        # Set config defaults
        config_defaults = {}
        config = {**config_defaults, **config}
        self.modify_dask_config(config)

        # Set LocalCluster defaults
        if "host" not in kwargs:
            kwargs["host"] = ""
        if memory_limit is not None:
            kwargs["memory_limit"] = memory_limit

        # Set up cluster, connect scheduler/client
        cluster = LocalCluster(**kwargs)
        client = Client(cluster)
        self.set_cluster(cluster)
        self.set_client(client)

class remote_cluster(_cluster):

    def __init__(self, cluster, config={}):
        # Initialize base class
        super().__init__()

        # Set config defaults
        config_defaults = {}
        config = {**config_defaults, **config}
        self.modify_dask_config(config)

        # Setup client
        client = Client(cluster)
        self.set_cluster(cluster)
        self.set_client(client)
