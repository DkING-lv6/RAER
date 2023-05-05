# Code for RAER
This repository is based on [ReMERT-pytorch](https://github.com/AIDefender/MyDiscor)[1]. 

## Setup (refer to the DisCor[2])
If you are using Anaconda, first create the virtual environment.

```bash
conda create -n discor python=3.8 -y
conda activate discor
```

Then, you need to setup a MuJoCo license for your computer. Please follow the instruction in [mujoco-py](https://github.com/openai/mujoco-py
) for help.

Finally, you can install Python liblaries using pip.

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

If you're using other than CUDA 10.2, you need to install PyTorch for the proper version of CUDA. See [instructions](https://pytorch.org/get-started/locally/) for more details.

# Training

```bash
python train.py --cuda --env_id Ant-v2 --config config/mujoco.yaml --num_steps 1000000 --algo discor --lfiw --tper --reweigh_type exp --seed 1000
```

## References
[1](https://arxiv.org/pdf/2105.07253v3.pdf) Xuhui Liu, Zhenghai Xue, Jingcheng Pang, Shengyi Jiang, Feng Xu and Yang Yu. "Regret Minimization Experience Replay in Off-Policy Reinforcement Learning". In Proceedings of the 35th Conference on Neural Information Processing Systems, pages 17604-17615, 2021.

[2](https://arxiv.org/abs/2003.07305) Kumar, Aviral, Abhishek Gupta, and Sergey Levine. "Discor: Corrective feedback in reinforcement learning via distribution correction." In Proceedings of the 34th Conference on Neural Information Processing Systems, pages 18560-18572, 2020.