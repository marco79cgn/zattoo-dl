# zattoo-dl
A tool which lists all Zattoo recordings and downloads selected items.

## Docker
A docker container can be built in three easy steps: 

1. Checkout: 
```
git clone https://github.com/marco79cgn/zattoo-dl.git
```
2. Build Docker image: 
```
cd zattoo-dl
docker build -t zattoo-dl .
```
3. Download content: 
```
docker run --rm -it -v $(pwd)/:/data zattoo-dl -u 'username' -p 'password'
```
In this example the current directory will be used `$(pwd)` where the command was executed. The downloads will be placed there as well. Instead of `$(pwd)` it's also possible to use a full qualified path: `-v /Users/marco/movies/zattoo:/data`


### CLI Parameters

This script supports the following parameters:

| Parameter | Short | Description | Example | Default / Required |
|-----------|-------|-------------|---------|------------------|
| `--username` | `-u` | Your Zattoo username | `-u "user@example.com"` | **Required** |
| `--password` | `-p` | Your Zattoo password | `-p "mypassword"` | **Required** |
| `--filter` | `-f` | Search by title or episode (case-insensitive) | `-f "Tagesschau"` | Empty = no filtering |
| `--bilingual` | `-b` | Enable download with multiple audio streams | `-b` | disabled by default |
| `--external-dl` | `-e` | Forward Download to Metube (external tool) | `-e "192.168.178.14:8086"` | Empty = internal download via `ffmpeg`/`yt-dlp` |
| `--limit-results` | `-l` | Number of recordings to display. Negative = last n recordings | `-l 10` or `-l -10` | default: show all recordings |

### Notes on Required Parameters

- **Username (`-u`)** and **Password (`-p`)** are always required.  
- All other parameters are optional.