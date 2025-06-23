# cnu-sm-research
All things related to social media research performed by CNU School of Medicine.

## Setup
Requires Python 3.7+ probably. Haven't verified personally but it probably works.
Download `keys.json` from [Google Drive](https://drive.google.com/file/d/1iy0SgMLE9nUbWr27QuKhzAIPczcJkCRO/view?usp=drive_link); request access.

In the Mac Terminal, enter these commands:
```
// Copy this code into your computer
git clone git clone git@github.com:chriswxho/cnu-sm-research.git
cd cnu-sm-research

// Move `keys.json` into the newly created `cnu-sm-research` directory.

// Create a virtual environemnt to manage package deps.
python3 -m venv cnu-sm-research
source cnu-sm-research/bin/activate
pip3 install -r requirements.txt

// Open JupyterLab. It will create a webpage with a notebook UI, go to `playground.ipynb` to experiment!
jupyter lab
```

