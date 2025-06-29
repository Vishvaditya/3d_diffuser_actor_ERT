"""Modified from
https://github.com/mees/calvin/blob/main/calvin_models/calvin_agent/evaluation/evaluate_policy.py
"""
import os
import sys
import gc
from typing import Tuple, Optional, List
import random
import logging
import datetime
from pathlib import Path

import tap
import hydra
from omegaconf import OmegaConf
import torch
import numpy as np
import yaml
from tqdm import tqdm

from utils.common_utils import get_gripper_loc_bounds
from online_evaluation_calvin.evaluate_model import create_model
from online_evaluation_calvin.evaluate_utils import (
    prepare_visual_states,
    prepare_proprio_states,
    count_success,
    get_env_state_for_initial_condition,
    collect_results,
    write_results,
    get_log_dir
)
from online_evaluation_calvin.multistep_sequences import get_sequences
from online_evaluation_calvin.evaluate_utils import get_env

EP_LEN = 60
NUM_SEQUENCES = 10
EXECUTE_LEN = 20
ANNOT_NUM = 0


logging.basicConfig(
    filename=f"logs/{datetime.datetime.now()}_{NUM_SEQUENCES}.log",  # Path to your log file
    filemode="a",               # Append mode; use "w" to overwrite each time
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG          # Or DEBUG, WARNING, ERROR, etc.]
)

logger = logging.getLogger(__name__)

def log_print(*args, level=logging.INFO, sep=' ', end='\n'):
    message = sep.join(str(arg) for arg in args) + end
    logger.log(level, message.strip())




class Arguments(tap.Tap):
    # Online enviornment
    calvin_dataset_path: Path = "/home/tsungwek/repos/calvin/dataset/task_ABC_D"
    calvin_model_path: Path = "/home/tsungwek/repos/calvin/calvin_models"
    calvin_demo_tasks: Optional[List[str]] = None
    device: str = "cuda"
    text_encoder: str = "clip"
    text_max_length: int = 16
    save_video: int = 0

    # Offline data loader
    seed: int = 0
    tasks: Tuple[str, ...] # indicates the environment
    checkpoint: Path
    gripper_loc_bounds: Optional[str] = None
    gripper_loc_bounds_buffer: float = 0.04
    calvin_gripper_loc_bounds: Optional[str] = None
    relative_action: int = 0

    # Logging to base_log_dir/exp_log_dir/run_log_dir
    base_log_dir: Path = Path(__file__).parent / "eval_logs" / "calvin"

    # Model
    action_dim: int = 7 # dummy, as DiffuserActor assumes action_dim is 7
    image_size: str = "256,256" # decides the FPN architecture
    backbone: str = "clip"  # one of "resnet", "clip"
    embedding_dim: int = 120
    num_vis_ins_attn_layers: int = 2
    use_instruction: int = 0
    rotation_parametrization: str = 'quat'
    quaternion_format: str = 'wxyz'
    diffusion_timesteps: int = 100
    lang_enhanced: int = 0
    fps_subsampling_factor: int = 3
    num_history: int = 0
    interpolation_length: int = 2 # the number of steps to reach keypose
    mode: str = "normal"    # Running mode "ert" or "normal"
    annot_dir: Path = "annotations"


def make_env(dataset_path, show_gui=True, split="validation", scene=None):
    val_folder = Path(dataset_path) / f"{split}"
    if scene is not None:
        env = get_env(val_folder, show_gui=show_gui, scene=scene)
    else:
        env = get_env(val_folder, show_gui=show_gui)

    return env


def evaluate_policy(model, env, conf_dir, annot_dir, eval_log_dir=None, save_video=False,
                    sequence_indices=[]):
    """
    Run this function to evaluate a model on the CALVIN challenge.

    Args:
        model: an instance of CalvinBaseModel
        env: an instance of CALVIN_ENV
        conf_dir: Path to the directory containing the config files of CALVIN
        eval_log_dir: Path where to log evaluation results
        save_video: a boolean indicates whether to save the video
        sequence_indices: a list of integers indicates the indices of the
            instruction chains to evaluate

    Returns:
        results: a list of integers indicates the number of tasks completed
    """
    task_cfg = OmegaConf.load(conf_dir / "callbacks/rollout/tasks/new_playtable_tasks.yaml")
    task_oracle = hydra.utils.instantiate(task_cfg)

    # Updating val annotations input dynamically based on mode
    if args.mode=="normal":
        val_annotations = OmegaConf.load(annot_dir / "new_playtable_validation.yaml")
        print("Annotation Directory: ", os.path.join(annot_dir/"new_playtable_validation.yaml"))
    elif args.mode == "ert":
        val_annotations = OmegaConf.load(annot_dir / "ert_playtable_instructions.yaml")
        print("Annotation Directory: ", os.path.join(annot_dir/"ert_playtable_instructions.yaml"))

    elif args.mode == "descriptive":
        val_annotations = OmegaConf.load(annot_dir / "descriptive_playtable_instructions.yaml")
        print("Annotation Directory: ", os.path.join(annot_dir/"descriptive_playtable_instructions.yaml"))

    elif args.mode == "test":
        val_annotations = OmegaConf.load(annot_dir / "descriptive_test.yaml")
        print("Annotation Directory: ", os.path.join(annot_dir/"descriptive_test.yaml"))
        save_video = 0

    eval_log_dir = get_log_dir(eval_log_dir)

    eval_sequences = get_sequences(NUM_SEQUENCES)

    results, tested_sequence_indices = collect_results(eval_log_dir, args.mode, args.seed, NUM_SEQUENCES, ANNOT_NUM) # Added mode and num_sequences variables to filenames

    for seq_ind, (initial_state, eval_sequence) in enumerate(eval_sequences):
        if sequence_indices and seq_ind not in sequence_indices:
            continue
        if seq_ind in tested_sequence_indices:
            continue
        result, subtask, lang_annotation, videos = evaluate_sequence(
            env, model, task_oracle, initial_state,
            eval_sequence, val_annotations, save_video, ANNOT_NUM
        )
        write_results(eval_log_dir, seq_ind, result, 
                      subtask, lang_annotation, args.mode, args.seed, NUM_SEQUENCES, ANNOT_NUM) # Added mode and num_sequences variables to filenames # Also added subtask and lang annotations
        results.append(result)
        str_results = (
            " ".join([f"{i + 1}/5 : {v * 100:.1f}% |"
            for i, v in enumerate(count_success(results))]) + "|"
        )
        print(str_results + "\n")

        # Added video save feature
        if save_video:
            np.savez_compressed(f"result_videos/video_{args.mode}_seed_{args.seed}_task_{subtask}_annot_{ANNOT_NUM}.npz", video=videos)#f"result_{mode}_seed_{seed}_annot_{annot_num}.txt


    return results


def evaluate_sequence(env, model, task_checker, initial_state, eval_sequence,
                      val_annotations, save_video, annot_num):
    """
    Evaluates a sequence of language instructions.

    Args:
        env: an instance of CALVIN_ENV
        model: an instance of CalvinBaseModel
        task_checker: an indicator of whether the current task is completed
        initial_state: a tuple of `robot_obs` and `scene_obs`
            see: https://github.com/mees/calvin/blob/main/dataset/README.md#state-observation
        eval_sequence: a list indicates the instruction chain
        val_annotations: a dictionary of task instructions
        save_video: a boolean indicates whether to save the video

    Returns:
        success_counter: an integer indicates the number of tasks completed
        video_aggregator: a list of lists of images that shows the trajectory
            of the robot

    """
    robot_obs, scene_obs = get_env_state_for_initial_condition(initial_state)
    env.reset(robot_obs=robot_obs, scene_obs=scene_obs)

    success_counter, video_aggregators = 0, []
    for subtask in eval_sequence:
        # get lang annotation for subtask
        lang_annotation = val_annotations[subtask][annot_num]
        success, video = rollout(env, model, task_checker,
                                 subtask, lang_annotation)
        video_aggregators.append(video)

        if success:
            success_counter += 1
            print("Task Successful")
        else:
            print("Task Failed")
            return success_counter, subtask, lang_annotation, video_aggregators
    
    return success_counter, subtask, lang_annotation, video_aggregators # added subtask, lang_annotation


def rollout(env, model, task_oracle, subtask, lang_annotation):
    """
    Run the actual rollout on one subtask (which is one natural language instruction).

    Args:
        env: an instance of CALVIN_ENV
        model: an instance of CalvinBaseModel
        task_oracle: an indicator of whether the current task is completed
        subtask: a string indicates the task name
        lang_annotation: a string indicates the instruction of the task

    Returns:
        Success/Fail: a boolean indicates whether the task is completed
        video: a list of images that shows the trajectory of the robot
    """
    video = [] # show video for debugging
    obs = env.get_obs()

    model.reset()
    start_info = env.get_info()

    print('------------------------------')
    print(f'task: {lang_annotation}')
    video.append(obs["rgb_obs"]["rgb_static"])

    pbar = tqdm(range(EP_LEN))
    for step in pbar:
        obs = prepare_visual_states(obs, env)
        obs = prepare_proprio_states(obs, env)
        lang_embeddings = model.encode_instruction(lang_annotation, model.args.device)
        with torch.cuda.amp.autocast():
            trajectory = model.step(obs, lang_embeddings)
        for act_ind in range(min(trajectory.shape[1], EXECUTE_LEN)):
            # calvin_env executes absolute action in the format of:
            # [[x, y, z], [euler_x, euler_y, euler_z], [open]]
            curr_action = [
                trajectory[0, act_ind, :3],
                trajectory[0, act_ind, 3:6],
                trajectory[0, act_ind, [6]]
            ]
            pbar.set_description(f"step: {step}")
            curr_proprio = obs['proprio']
            obs, _, _, current_info = env.step(curr_action)
            obs['proprio'] = curr_proprio

            # check if current step solves a task
            current_task_info = task_oracle.get_task_info_for_set(
                start_info, current_info, {subtask}
            )

            video.append(obs["rgb_obs"]["rgb_static"])

            if len(current_task_info) > 0:
                return True, video

    return False, video


def get_calvin_gripper_loc_bounds(args):
    with open(args.calvin_gripper_loc_bounds, "r") as stream:
       bounds = yaml.safe_load(stream)
       min_bound = bounds['act_min_bound'][:3]
       max_bound = bounds['act_max_bound'][:3]
       gripper_loc_bounds = np.stack([min_bound, max_bound])

    return gripper_loc_bounds


def main(args):

    # These location bounds are extracted from language-annotated episodes
    if args.gripper_loc_bounds is None:
        args.gripper_loc_bounds = np.array([[-2, -2, -2], [2, 2, 2]]) * 1.0
    else:
        args.gripper_loc_bounds = get_gripper_loc_bounds(
            args.gripper_loc_bounds,
            task=args.tasks[0] if len(args.tasks) == 1 else None,
            buffer=args.gripper_loc_bounds_buffer,
        )

    # These location bounds are extracted from every episode in play trajectory
    if args.calvin_gripper_loc_bounds is not None:
        args.calvin_gripper_loc_bounds = get_calvin_gripper_loc_bounds(args)

    # set random seeds
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    # evaluate a custom model
    model = create_model(args)

    sequence_indices = [
        i for i in range(args.local_rank, NUM_SEQUENCES, int(os.environ["WORLD_SIZE"]))
    ]

    env = make_env(args.calvin_dataset_path, show_gui=False)
    evaluate_policy(model, env,
                    conf_dir=Path(args.calvin_model_path) / "conf",
                    annot_dir=Path(args.annot_dir), # Updated annotations path
                    eval_log_dir=args.base_log_dir,
                    sequence_indices=sequence_indices,
                    save_video=args.save_video)

    results, sequence_inds = collect_results(args.base_log_dir, args.mode, args.seed, NUM_SEQUENCES, ANNOT_NUM) # Added mode and num sequences to filename
    str_results = (
        " ".join([f"{i + 1}/5 : {v * 100:.1f}% |"
        for i, v in enumerate(count_success(results))]) + "|"
    )
    print(f'Load {len(results)}/1000 episodes...')
    print(str_results + "\n")

    del env
    gc.collect()

if __name__ == "__main__":
    args = Arguments().parse_args()
    args.local_rank = 0 #int(os.environ["LOCAL_RANK"])
    # print("--------------", args.local_rank, "---------------")

    # DDP initialization
    torch.cuda.set_device(args.local_rank)
    torch.distributed.init_process_group(backend='nccl', init_method='env://')
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = True

    print("Running Mode: ", args.mode)
    print("Number of sequences: ", NUM_SEQUENCES)
    print("Annotation Number: ", ANNOT_NUM)

    log_print("Running Mode: " + str(args.mode))
    log_print("Number of sequences: "+ str(NUM_SEQUENCES))
    log_print("Annotation Number: "+str(ANNOT_NUM))
    main(args)
