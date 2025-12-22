# zattoo-dl
A tool which lists all Zattoo recordings and downloads selected items.

<img src="https://github.com/user-attachments/assets/2b21c5ac-26a3-45ba-953f-07905565851a" width="850"/>

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
In this example the current directory will be used `$(pwd)` where the command was executed. The downloads will be placed there as well. Be aware that `$(pwd)` is only availably on Linux/macOS. On Windows, always use the full qualified path with backslashes. Always make sure that these directories exist on the host and always mount them to `/data` inside the container: 

|   platform  |        	path       		| 	 description      |
|-------------|-------------------------|---------------------|
| Linux/macOS | `$(pwd):/data` 	   			| current directory (where the docker command is executed) |
| Linux/macOS | `/Users/marco/zattoo:/data`   | full path with `/` (slashes) |
| Windows     | `C:\Users\marco\zattoo:/data` | full path with drive letter and `\` (backslashes) |

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

- **Username (`-u`)** and **Password (`-p`)** are always required
- all other parameters are optional
- `ffmpeg` is used by default and is the fastest option since it downloads all at once (one video and one audio stream)
- for multiple audio languages (`--bilingual`) and embedded optional subtitles, `yt-dlp` will be used instead because `ffmpeg` can't handle embedded subtitles in `WebVTT` format
- `yt-dlp` takes more time and leads to more i/o since every single audio, video and subtitle stream will be downloaded one after another (and multiplexed at the end)
- when using `metube` as external yt-dlp downloader, make sure that it doesn't start too many downloads in parallel (because your Zattoo subscription only offers 1-4 streams at the same time)

## Optional: use Github Docker image

Instead of building the docker image yourself, you can use the existing one from this Github Repository: `ghcr.io/marco79cgn/zattoo-dl`. 

No need to install or build anything in this case. Just run: 
```
docker run --rm -it -v "$(pwd)":/data ghcr.io/marco79cgn/zattoo-dl -u 'username' -p 'password'
```

## Optional: run script natively (macOS & Linux)

Docker is the recommended and easiest way. But it's also possible to run the script natively. The following tools have to be installed:

- `curl` for http/api requests
- `ffmpeg`
- `yt-dlp` (includes ffmpeg)
- `jq` as JSON command line parser
- `gnu-tools` (echo, grep, awk, sed)
