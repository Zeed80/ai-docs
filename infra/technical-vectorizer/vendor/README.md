# Repository for the course "Theoretical foundation of data science"

Base code was takwn from the [official repository](https://github.com/Vahe1994/Deep-Vectorization-of-Technical-Drawings) .
Code developed here is also commited to the main repository.


## Dataset
Scripts to download dataset are in folder dataset/.
* For ABC,real datasets use download_dataset.sh
* For PFP, use precision_floorplan_download.py  
  Read ReadMe there for more instructions.

## Compare

To compare with us without running code, you can download our results on the full pipeline on the test set
for [pfp](https://drive.google.com/file/d/1FGm-JQsvOa5sbi_f_-MMl1XC5Z8JGe0F/view?usp=sharing) and for
[abc](https://drive.google.com/file/d/1lR5lea3sY4Bhp9QL4MmmPs0kqZ5voPGu/view?usp=sharing).

## Notebooks

To show how some of the usability of the functions, there are several notebooks in the notebooks folder.
1) Rendering notebook
2) Dataset loading, model loading, model training, loss function loading
3) Notebook that illustrates  how to work with pretrained model and how to do refinement on lines(without merging)
4) Notebook that illustrates how to work with pretrained model and how to do refinement on curves(without merging)

## Requirments
Linux system  
Python 3

See requirments.txt and additional packages

cairo==1.14.12  
pycairo==1.19.1  
chamferdist==1.0.0  

## Models

Download pretrained models for [curve](https://drive.google.com/file/d/18jN37pMvEg9S05sLdAznQC5UZDsLz-za/view?usp=sharing)
and for [line](https://drive.google.com/file/d/1Zf085V3783zbrLuTXZxizc7utszI9BZR/view?usp=sharing) .

## Dockerfile

Build the docker image:

```bash
docker build -t Dockerfile owner/name:version .
```
example:
```bash
docker build -t vahe1994/deep_vectorization:latest .
```


When running container mount folder with reporitory into code/, folder with datasets in data/ folder with logs in logs/
```bash
docker run --rm -it --shm-size 128G -p 4045:4045 --mount type=bind,source=/home/code,target=/code --mount type=bind,source=/home/data,target=/data --mount type=bind,source=/home/logs,target=/logs  --name=container_name owner/name:version /bin/bash
```

Anaconda with packages is installed in follder opt/ . Environement with packages that needed is installed in environment vect-env.
. To activate it run in container
```bash
. /opt/.venv/vect-env/bin/activate/
```



## How to run
1. Download models.
2. Either use Dockerfile to create docker image with needed environment or just install requirements
3. Run scripts/run_pipeline.sh with correct paths for trained model, data dir and output dir. Don't forget to chose primitive type and primitive count in one patch. 



## How to train
Look at vectorization/srcipts/train_vectorizatrion (currently under refactoring)


##  Table to take track of what was done from tasks

More information about tasks could be found in section 4 from the report below.

| Task №       | Status         |
| ------------- |:-------------:|
| visualization | yes        |
| requirement    | added        |
| setup.py    | added and tested|
| jupyter notebooks    |added and tested|
| documentation | partly|
|pypeline eval|yes|
|models|yes|
|train.py refactor|partly|
|functions descriptions|partly|
|docker file|added and tested|



Below you can find a project report for the course. 


## Project report in readme 

<b>1. Problem statement</b>
Writing research code is much different than writing code for the product. In research, one part of code can be written 
and rewritten multiple times with drastic changes, one more significant difference is that, code often written in a 
hurry in notebooks(for convenience and fast experiments) and only part was in file format. 
Because code is rapidly involving and changing it is hard to maintain quality. Not helping the fact that different 
people writing code in different style and quality. 
Forcing one style is hard when there is not much time till a deadline. In the end, you end up with a lot of code and 
branches that should be refactored and rewritten before publishing the code to the public.  
The goal of this project is to make an easily reproducible repository (more precisely continue upgrading existing) 
and good documentation for public use. We have 2 repositories with 15 branches of research code from the ECCV2020 paper 
 Deep vectorization of technical drawings and repository in  Github repository, with more than already 22000 lines of 
code, to combine all the parts. We would like to make the code easy to use and understand and mainly easy to run.   


<b> 2. Main challenges</b>

Combine code from different branches that are not compatible and make an easily readable and reproducible code. Another 
challenge is to make documentation for functions in the code.    

<b>3.Description of a baseline solution. Some other implementations which will serve as inspiration or baseline for your work</b>  
The baseline solution is to release the initial code(code form research) without refactoring(with all branches). 
Another solution is to live the already refactored repository as it is, without any further improvement and commits.  

<b> 3.1. Pros and cons of these solutions</b>

1. Releasing research repositories:   
     Pros: All raw code would be available to anyone with all experiments and legacy code.  
     Cons: Code is a research mess.  Almost nobody would try to understand or try to use it.  
2. Keeping refactored repository as it is:  
     Pros: Time savings.  
     Cons: Not all functionality carefully present and well documented(No good documentation, no docker file, and e.t.c. for more detail look at https://github.com/Vahe1994/Deep-Vectorization-of-Technical-Drawings )  

<b>3.2. Ideas on how to improve it or how you are going to use it.</b>    
     Add Jupyter notebooks with an explanation of how to evaluate functions, add a docker file, and a list of 
     requirements. Add trained models and documentations. For more details look at the list below.
  
<b>4.Roles for the participants</b>  
Because this team consists of only one member, all proposed tasks would be done by me. It includes writing project reports, code, and documentation. In the list below you can find a brief description of tasks:

1. Create code for visualization(rewrite tensorboard code to make it work or use wandb).   
2. Create a python script for evaluating models on images. 
3. Make Jupyter notebooks to show how to use different parts of pipelines with descriptions. 
4. Create documentation for the repository. 
5. Make description for most used functions 
6. Make a docker file or docker image for the repository. 
7. Make available models(some of them should be trained again). 
8. Make setup.py. 
9. Make requirements document.  
10. Correct train.py to make it work and where possible refactor code.

<b> 5.Link to the GitHub repository</b>  
1) Github repository for the course - https://github.com/adasegroup/Deep-vectorization-P.R.
2) Official  Github repository for article Deep Vectorization of Technical Drawings  - https://github.com/Vahe1994/Deep-Vectorization-of-Technical-Drawings



<b>6. Project Structure </b>

The project has a module-like structure.
The main modules are cleaning, vectorization, refinement, and merging(each module has an according to folder). 
Each folder has Readme with more details. Here is the brief content of each folder.

* cleaning - model, script to train and run, script to generate synthetic data
* vectorization - NN models, script to train
* refinement - refinement module for curves and lines
* merging - merging module for curves and lines
* dataset - scripts to download ABC, PFP, cleaning datasets, scripts to modify data into patches, and memory-mapped them.
* notebooks - a playground to show some function in action.
* utils - loss functions, rendering, metrics
* scripts - scripts to run training and evaluation


<b> 7. Evaluation results </b>  

Look at notebooks pretrain_model_loading_and_evaluation_for_line.ipynb and
pretrain_model_loading_and_evaluation_for_curve.ipynb , for an example how to run primitive estimation
and refinement for curve and line.

P.s. For results on bigger datasets please look at the according to paper in the section with evaluation(p 11, table 1.).

<b>References:</b>  
1) V. Egiazarian, O. Voynov, A. Artemov, D. Volkhonskiy, A. Safin, M. Taktasheva, D. Zorin, and E. Burnaev.
Deep vectorization of technical drawings. arXiv preprint arXiv:2003.05471, 2020
2)  Wandb site - https://wandb.ai/site