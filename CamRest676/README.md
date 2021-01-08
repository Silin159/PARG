# PARG with TSCP on CamRest676

## Construct Paraphrase for CamRest676

```
python analysis.py
```

## Training with default parameters

```
python model.py -mode train -model tsdf-camrest
```

(optional: configuring hyperparameters with cmdline)

```
python model.py -mode train -model tsdf-camrest -cfg lr=0.003 batch_size=32
```

## Testing

```
python model.py -mode test -model tsdf-camrest
```

## Reinforcement fine-tuning

```
python model.py -mode rl -model tsdf-camrest -cfg lr=0.0001
```

## Before running
1. Install required python packages. We used pytorch 0.3.0 and python 3.6 under Linux operating system. 
```
pip install -r requirements.txt
```
2. CamRest676 dataset is placed in PROJECT_ROOT/data/CamRest676.

3. Make directories under PROJECT_ROOT.
```
mkdir vocab
mkdir log
mkdir results
mkdir models
mkdir sheets
```

4. Download pretrained Glove word vectors and place them in PROJECT_ROOT/data/glove.
