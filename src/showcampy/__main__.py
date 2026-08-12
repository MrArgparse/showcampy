from bs4 import BeautifulSoup, Tag
from datetime import datetime
from fake_useragent import UserAgent
from mutagen.mp4 import MP4
from pathlib import Path
from rich.logging import RichHandler
from typing import Type
from urllib.parse import urlparse
import argparse
import logging
import msgspec
import platformdirs
import re
import requests
import subprocess
import sys
import time
import tomlkit

logging.basicConfig(
    level=logging.INFO, format='%(message)s', datefmt='[%X]', handlers=[RichHandler()]
)

PLATFORMDIRS = platformdirs.PlatformDirs(appname='showcampy', appauthor=False)

def is_unraid() -> bool:
    return Path("/etc/unraid-version").is_file()

if is_unraid():
    CONFIG_FOLDER = Path("/boot/config/showcampy")
else:
    CONFIG_FOLDER = PLATFORMDIRS.user_config_path

DEFAULT_CONFIGURATION_PATH = CONFIG_FOLDER / "showcampy_config.toml"
DEFAULT_ENCODING = "utf-8"
DEFAULT_SAVE_PATH = PLATFORMDIRS.user_downloads_path / "showcamrips"
DEFAULT_ARCHIVES_FOLDER = DEFAULT_SAVE_PATH / "videos_archives"
MAIN_URL = "https://www.showcamrips.com/"
SMOVIES_SUBSTRING = "/show-cam-sex-movies/"
UA_OBJ = UserAgent()
UA = UA_OBJ.chrome
PAGE_HOSTING_REFERER = "https://mixdrop.ag/"
PLAYER_ORIGIN_REFERER = "https://miixdrop.net/"
MIXDROP_TEST_HEADERS = {"User-Agent": UA, "Referer": PAGE_HOSTING_REFERER}
MIXDROP_DL_HEADERS = {
    "User-Agent": UA,
    "Referer": PLAYER_ORIGIN_REFERER,
    "Origin": PLAYER_ORIGIN_REFERER.strip("/"),
    "Accept-Encoding": "identity",
}
SHOWCAMRIPS_HEADERS = {"User-Agent": UA, "Referer": MAIN_URL}
SESSION = requests.Session()
ERR_DL = "Download failed after retries"
ERR_K_LIST = "k-list not found"
ERR_PACKED_STR = "Packed string not found"
ERR_MP4_URL = "MP4 URL not found"
ERR_BAD_STATUS = "Bad status: {}"
ERR_SORRY = "WE ARE SORRY"
ERR_VIDEO_NOT_FOUND = f"Video not found: Msg: {ERR_SORRY}, We can't find the video you are looking for."
ERR_VIDEO_LINK = "Could not get video link: Status code: {}"
ERR_SOURCE_WEBSITE = "Source website not found"
INFO_RETRY = "Retry {}/4 due to: {}"
INFO_URL = "Main URL: {}"
INFO_PERFORMER = "Fetching performer info"
INFO_LINKS = "Fetching links from page {} out of {}"
INFO_VIDEO_IDX = "Video {} out of {}: {}"
INFO_PLAY_LINK = "Fetching play link from: {}"
INFO_SRC = "Fetching src from: {}"
INFO_META = "Embedding metadata"
INFO_ARCHIVE = "Archiving"
INFO_PRESENT_IN_ARCHIVE = "Video already in archive"
INFO_FINISHED = "Finished downloading playlist"
INFO_DEST = "\r[download] Destination: {} "


class MixdropError(Exception):
    pass


class DownloadError(MixdropError):

    def __init__(self) -> None:
        super().__init__(ERR_DL)


class BadStatusError(MixdropError):

    def __init__(self, status_code: int) -> None:
        super().__init__(ERR_BAD_STATUS.format(status_code))
        self.status_code = status_code


class DefaultConfig(msgspec.Struct, kw_only=True):
    downloads_folder: Path = DEFAULT_SAVE_PATH
    archives_folder: Path = DEFAULT_ARCHIVES_FOLDER


def parse_showcampy() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="showcampy")
    parser.add_argument("url", nargs="+", help="url")
    return parser


def encode_hook(obj: Path | str) -> str:
    if isinstance(obj, Path):
        return str(obj)

    return obj


def decode_hook(type_: Type[Path], value: Path | str) -> Path | str:
    if type_ is Path and isinstance(value, str):
        return Path(value)

    return value


def get_config_path(path: Path | None = None) -> Path:
    if path is None:
        return DEFAULT_CONFIGURATION_PATH

    return path


def load_config(path: Path | None = None) -> DefaultConfig:
    path = get_config_path(path)

    with open(path, "r", encoding=DEFAULT_ENCODING) as fp:
        data = fp.read()

    config_dict = tomlkit.loads(data)

    try:
        return msgspec.convert(config_dict, type=DefaultConfig, dec_hook=decode_hook)
    except msgspec.DecodeError:
        return DefaultConfig()


def save_config(configuration: DefaultConfig, path: Path | None = None) -> None:
    path = get_config_path(path)
    data = tomlkit.dumps(msgspec.to_builtins(configuration, enc_hook=encode_hook))
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding=DEFAULT_ENCODING) as fp:
        fp.write(data)


def load_or_create_config(path: Path | None = None) -> DefaultConfig:
    path = get_config_path(path)

    try:
        return load_config(path)
    except FileNotFoundError:
        pass

    configuration = DefaultConfig()
    save_config(configuration, path)
    return configuration


CONFIG = load_or_create_config()
DL_PATH = CONFIG.downloads_folder
ARCHIVES_FOLDER = CONFIG.archives_folder


def check_path(CONFIG: DefaultConfig) -> None:
    for key in CONFIG.__annotations__.keys():
        path = CONFIG.__getattribute__(key)
        path.mkdir(parents=True, exist_ok=True)


check_path(CONFIG)


def get_document(url: str) -> BeautifulSoup:
    r = SESSION.get(url, headers=SHOWCAMRIPS_HEADERS)
    r.raise_for_status()
    return BeautifulSoup(r.content, "html.parser")


def get_performer_pages(soup: BeautifulSoup) -> tuple[list[str], int]:
    pages_elements = soup.find(class_="pages")
    
    if isinstance(pages_elements, Tag):
        pages = [
            href
            for a in pages_elements.find_all("a")
            if isinstance(a, Tag)
            and isinstance((href := a.get("href")), str)
        ]

        return pages, len(pages)

    return [], 0


def get_all_page_urls(soup: BeautifulSoup) -> list[str]:
    return [
        href
        for ele in soup.find_all(class_="moiclick1")
        if isinstance(ele, Tag) 
        and isinstance((href := ele.get("href")), str)
    ]


def get_last_url_segment(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path.rstrip("/").split("/")[-1]


def build_command(
    url: str,
    video_download_path: Path,
) -> list[str | Path]:
    return [
        "yt-dlp", url,
        "--no-warnings",
        "--add-header", f"Referer: {MAIN_URL}",
        "--abort-on-unavailable-fragments",
        "--ignore-config",
        "-N", "2",
        "--file-access-retries", "4",
        "--retries", "100",
        "--retry-sleep", "2",
        "--user-agent", UA,
        "-o", video_download_path
    ]


def read_archive(archive: Path) -> list[int]:
    with open(archive, 'r') as file:
        id_list = [int(line.split()[1]) for line in file if line.strip()]

    return id_list


def save_txt(path_name: Path, text_string: str) -> None:
    path_name.parent.mkdir(parents=True, exist_ok=True)

    with open(path_name, "a", encoding="utf-8") as txt_file:
        txt_file.write(text_string)


def get_source_website(soup: BeautifulSoup) -> str | None:
    span = soup.find("span", class_="tl")

    if isinstance(span, Tag):
        source_website_element = span.find("a", href=lambda h: h and "/site/" in h)

        if source_website_element:
            return str(source_website_element.text)
    
    return None


def test_for_status_showcamrips(src: str) -> int:
    test_request = SESSION.head(src, headers=SHOWCAMRIPS_HEADERS)
    return test_request.status_code


def test_for_status_mixdrop(src: str) -> int:
    test_request = SESSION.head(src, headers=MIXDROP_TEST_HEADERS, allow_redirects=True)
    return test_request.status_code


def get_text_content_mixdrop(src: str) -> str:
    test_request = SESSION.get(src, headers=MIXDROP_TEST_HEADERS)
    return test_request.text


def test_for_content_mixdrop(text_content: str) -> bool:
    content_presence = True

    if ERR_SORRY in text_content:
        content_presence = False

    return content_presence


def get_mixdrop_video_link(text_content: str) -> str:
    k_match = re.search(r"'([^']+)'\.split\('\|'\)", text_content)

    if not k_match:
        raise Exception(ERR_K_LIST)

    k = k_match.group(1).split("|")
    p_match = re.search(r"eval\(function\(p,a,c,k,e,d\).*?\('([^']+)'\s*,", text_content, re.DOTALL)

    if not p_match:
        raise Exception(ERR_PACKED_STR)

    p = p_match.group(1)

    def replace_in_js_string(match: re.Match[str]) -> str:
        i = int(match.group(0))
        return k[i] if i < len(k) else match.group(0)

    resolved = re.sub(r"\b\d+\b", replace_in_js_string, p)
    url_match = re.search(r'//[^"]+\.mp4[^"]+', resolved)

    if not url_match:
        raise Exception(ERR_MP4_URL)

    return "https:" + url_match.group(0).replace("&amp;", "&")


def download_mixdrop_video(url: str, filename: Path) -> None:
    headers = MIXDROP_DL_HEADERS.copy()

    for attempt in range(4):
        existing_size = filename.stat().st_size if filename.exists() else 0

        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"

        try:
            with SESSION.get(url, headers=headers, stream=True) as r:

                if r.status_code in (200, 206):
                    content_length = r.headers.get("Content-Length")

                    if content_length:
                        total_size: int|None = int(content_length)
                        total_size = total_size + existing_size if total_size else None
                        mode = "ab" if existing_size > 0 else "wb"
                        print(INFO_DEST.format(filename))

                        with open(filename, mode) as f:
                            downloaded = existing_size
                            start = time.time()

                            for chunk in r.iter_content(8192):

                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)

                                    if total_size:
                                        percent = downloaded / total_size * 100
                                        speed = downloaded / (time.time() - start + 1e-6) / 1024
                                        print(
                                            f"\r{' ' * 100}\r[download] {percent:.1f}% "
                                            f"{downloaded//1024//1024}MB / {total_size//1024//1024}MB "
                                            f"{speed:.1f} KB/s",
                                            end=""
                                        )

                        print()
                        return

                else:
                    raise BadStatusError(r.status_code)

        except Exception as e:
            logging.warning(INFO_RETRY.format(attempt+1, e))
            time.sleep(2)

    raise DownloadError()


def get_showcamrips_video_link(src: str) -> str | None:
    actual_video_link = None
    soup = get_document(src) 
    video_tag = soup.find(id="myVideo")

    if isinstance(video_tag, Tag):
        actual_video_link = str(video_tag.get("src"))

    return actual_video_link


def get_play_video_link(soup: BeautifulSoup) -> str | None:
    play_video_link = None    
    iframe = soup.find("iframe")

    if isinstance(iframe, Tag):
        src = str(iframe.get("src"))

        if src:

            if "loading_video" in src:
                src = src.replace("loading_video", "play")
       
            play_video_link = src

    return play_video_link


def extract_datetime(s: str) -> str:
    match = re.search(r"(\d{4}-?\d{2}-?\d{2})[-_]?(\d{4,6})$", s)

    if match:
        date, time = match.groups()
        time = time.ljust(6, "0")
        joined_date_string  = re.sub(r"-", "", date + time)
        date = datetime.strptime(joined_date_string, "%Y%m%d%H%M%S")

        if date:
            formatted_date = date.strftime("%Y-%m-%d-%H-%M-%S")

    return formatted_date


def extract_video_id(s: str) -> int:
    match = re.match(r'^\d+', s)

    if match:
        group = match.group()

    return int(group)


def get_video_filename(performer: str, link: str) -> tuple[int, str]:
    last_segment = get_last_url_segment(link).rstrip(".html")
    video_id = extract_video_id(last_segment)
    formatted_date = extract_datetime(last_segment)
    filename = f"{performer} - {formatted_date} - {video_id}.mp4"
    return video_id, filename


def embed_comment(video_path: Path, comment: str) -> None:
    file = MP4(video_path)#type: ignore
    file["\xa9cmt"] = [f"{comment}"]
    file.save()#type: ignore


def touch_archive_path(performer_archive_path: Path) -> None:
    if not performer_archive_path.exists():
        save_txt(performer_archive_path, "")


def get_performer_name(soup: BeautifulSoup) -> str:
    performer = "NA"
    model_url_element = soup.find("a", href=lambda h: h and "/model/" in h)

    if isinstance(model_url_element, Tag):
        model_href = model_url_element.get("href")

        if isinstance(model_href, str):
            performer = get_last_url_segment(model_href)
    
    return performer


def build_url_list(url: str, soup: BeautifulSoup) -> list[str]:
    all_links = []

    if SMOVIES_SUBSTRING in url and url.endswith(".html"):
        all_links.append(url)
    else:
        logging.info(INFO_PERFORMER)
        page_links, total_pages = get_performer_pages(soup)

        for idx, page_link in enumerate(page_links):
            logging.info(INFO_LINKS.format((idx+1), total_pages))

            if page_link == page_links[0]:
                page_soup = soup
            else:
                page_soup = get_document(page_link)

            links = get_all_page_urls(page_soup)
            all_links.extend(links)
    
    return all_links


def main() -> None:
    parser = parse_showcampy()
    args = parser.parse_args(sys.argv[1:])

    for url in args.url:
        logging.info(INFO_URL.format(url))
        base_soup = get_document(url)
        performer = get_performer_name(base_soup)
        all_links = build_url_list(url, base_soup)
        total_all_links = len(all_links)
        performer_archive_path  = ARCHIVES_FOLDER / f"{performer}.txt"
        touch_archive_path(performer_archive_path)
        archive = read_archive(performer_archive_path)

        for idx, link in enumerate(all_links):
            video_id, video_filename = get_video_filename(performer, link)
            logging.info(INFO_VIDEO_IDX.format((idx+1), total_all_links, video_filename))

            if video_id not in archive:
                video_soup = get_document(link)
                source_website = get_source_website(video_soup)
                logging.info(INFO_PLAY_LINK.format(link))
                play_video_link = get_play_video_link(video_soup)
                logging.info(INFO_SRC.format(play_video_link))

                if play_video_link:

                    if "showcamrips" in play_video_link:            
                        status_code = test_for_status_showcamrips(play_video_link)

                    if "mixdrop" in play_video_link:
                        status_code = test_for_status_mixdrop(play_video_link)

                    if status_code != 200:
                        logging.error(ERR_VIDEO_LINK.format(status_code))
                        continue

                    if "showcamrips" in play_video_link: 
                        actual_video_link = get_showcamrips_video_link(play_video_link)

                    if "mixdrop" in play_video_link: 
                        text_content = get_text_content_mixdrop(play_video_link)
                        content = test_for_content_mixdrop(text_content)

                        if not content:
                            logging.error(ERR_VIDEO_NOT_FOUND)
                            continue

                        actual_video_link = get_mixdrop_video_link(text_content)

                if actual_video_link:            

                    if source_website:
                            sorted_download_path = DL_PATH / source_website / performer
                    else:
                        sorted_download_path = DL_PATH / performer
                        logging.warning(ERR_SOURCE_WEBSITE)

                    video_download_path = sorted_download_path / video_filename

                    if isinstance(actual_video_link, str) and isinstance(play_video_link, str) and "showcamrips" in play_video_link:
                        command = build_command(actual_video_link, video_download_path)
                        subprocess.run(command)

                    if isinstance(actual_video_link, str) and isinstance(play_video_link, str) and "mixdrop" in play_video_link:
                            video_download_path.parent.mkdir(parents=True, exist_ok=True)
                            download_mixdrop_video(actual_video_link, video_download_path)

                    if video_download_path.exists():
                        logging.info(INFO_META)
                        embed_comment(video_download_path, link)
                        logging.info(INFO_ARCHIVE)
                        save_txt(performer_archive_path, f"showcamrips {video_id}\n")

            else:
                logging.info(INFO_PRESENT_IN_ARCHIVE)

        logging.info(INFO_FINISHED)

if __name__ == "__main__":
    main()