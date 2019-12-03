# Setup on campus cluster
`module load python/3`
`module load git`

`git clone https://github.com/cupslab/neural_network_cracking.git && cd neural_network_cracking`

`mkdir -p /home/$USER/scratch/cups`
`pip install -t /home/$USER/scratch/cups  -r requirements-cpu.txt`
`pip install -t /home/$USER/scratch/cups  test-generator` (temporary until `requirements.txt` is updated in the original repository)

`export PYTHONPATH=/home/$USER/scratch/cups:${PYTHONPATH}`

## train 2-gram model with no smoothing
`python3 markov_model.py --train-file ../new_NEMO/NEMO/input/training.txt --ofile model.final --train-format list`

## train 6-gram model with additive smoothing
`python3 markov_model.py --train-file ../new_NEMO/NEMO/input/training.txt --ofile model-6-gram.final --train-format list --k-order 6 --smoothing 'additive'`

## to guess (2-gram):
`python3 markov_model.py --model-file model.final --k-order 2 --ofile markov_ofile.txt`

## to guess (6-gram) with additive smoothing:
`python3 markov_model.py --model-file model-6-gram-additive.final --k-order 6 --ofile markov_ofile.txt`

## Extras
### sort pwds by probability (desc)
`sort -gr -k2 -t$'\t' markov_ofile.txt -o sorted_markov_ofile.txt`

### first 5 likeliest pwd
`head -n5 sorted_markov_ofile.txt`