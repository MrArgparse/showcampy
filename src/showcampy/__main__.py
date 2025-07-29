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
import tomlkit

logging.basicConfig(
    level=logging.INFO, format='%(message)s', datefmt='[%X]', handlers=[RichHandler()]
)

PLATFORMDIRS = platformdirs.PlatformDirs(appname='showcampy', appauthor=False)
CONFIG_FOLDER = PLATFORMDIRS.user_config_path
DEFAULT_CONFIGURATION_PATH = CONFIG_FOLDER / 'showcampy_config.toml'
DEFAULT_ENCODING = 'utf-8'
DEFAULT_SAVE_PATH = PLATFORMDIRS.user_downloads_path / 'showcamrips'
DEFAULT_ARCHIVES_FOLDER = DEFAULT_SAVE_PATH / 'videos_archives'
MAIN_URL = 'https://www.showcamrips.com/'
UA_OBJ = UserAgent()
UA = UA_OBJ.chrome


class DefaultConfig(msgspec.Struct, kw_only=True):
    downloads_folder: Path = DEFAULT_SAVE_PATH
    archives_folder: Path = DEFAULT_ARCHIVES_FOLDER


def parse_showcampy() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='showcampy')
    parser.add_argument('url', nargs='+', help='url')
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

    with open(path, 'r', encoding=DEFAULT_ENCODING) as fp:
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

    with open(path, 'w', encoding=DEFAULT_ENCODING) as fp:
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
    r = requests.get(url)
    r.raise_for_status()
    return BeautifulSoup(r.content, "html.parser")


def get_performer_pages(soup: BeautifulSoup) -> tuple[list[str], int]:
    pages_elements = soup.find(class_='pages')
    
    if isinstance(pages_elements, Tag):
        pages = [
            href
            for a in pages_elements.find_all('a')
            if isinstance(a, Tag)
            and isinstance((href := a.get('href')), str)
        ]

        return pages, len(pages)

    return [], 0


def get_all_page_urls(soup: BeautifulSoup) -> list[str]:
    return [
        href
        for ele in soup.find_all(class_='moiclick1')
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
        'yt-dlp', url,
        '--no-warnings',
        '--user-agent', f'{UA}',
        '--add-header', f'Referer: {MAIN_URL}',
        '--abort-on-unavailable-fragments',
        '--ignore-config',
        '-N', '2',
        '--file-access-retries', '4',
        '--retries', '100',
        '--retry-sleep', '2',
        '--user-agent', UA,
        '-o', video_download_path
    ]


def read_archive(archive: Path) -> list[int]:
    with open(archive, 'r') as file:
        id_list = [int(line.strip('showcamrips').strip()) for line in file.readlines() if line]

    return id_list


def save_txt(path_name: Path, text_string: str) -> None:
    path_name.parent.mkdir(parents=True, exist_ok=True)

    with open(path_name, 'a', encoding='utf-8') as txt_file:
        txt_file.write(text_string)


def get_source_website(soup: BeautifulSoup) -> str | None:
    span = soup.find("span", class_="tl")

    if isinstance(span, Tag):
        source_website_element = span.find("a", href=lambda h: h and "/site/" in h)

        if source_website_element:
            return str(source_website_element.text)
    
    return None


def test_for_status(src: str) -> int:
    test_request = requests.head(src)
    return test_request.status_code


def get_actual_video_link(soup: BeautifulSoup) -> str | None:
    actual_video_link = None    
    iframe = soup.find('iframe')

    if isinstance(iframe, Tag):
        src = str(iframe.get('src'))

        if src:

            if 'loading_video' in src:
                src = src.replace('loading_video', 'play')
       
            actual_video_link = src

    return actual_video_link


def extract_datetime(s: str) -> str:
    match = re.search(r'(\d{4}-?\d{2}-?\d{2})[-_]?(\d{4,6})$', s)

    if match:
        date, time = match.groups()
        time = time.ljust(6, '0')
        joined_date_string  = re.sub(r'-', '', date + time)
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
    last_segment = get_last_url_segment(link).rstrip('.html')
    video_id = extract_video_id(last_segment)
    formatted_date = extract_datetime(last_segment)
    filename = f'{performer} - {formatted_date} - {video_id}.mp4'
    return video_id, filename


def embed_comment(video_path: Path, comment: str) -> None:
    file = MP4(video_path)#type: ignore
    file["\xa9cmt"] = [f'{comment}']
    file.save()#type: ignore


def touch_archive_path(performer_archive_path: Path) -> None:
    if not performer_archive_path.exists():
        save_txt(performer_archive_path, '')


def get_performer_name(soup: BeautifulSoup) -> str:
    performer = 'NA'
    model_url_element = soup.find("a", href=lambda h: h and "/model/" in h)

    if isinstance(model_url_element, Tag):
        model_href = model_url_element.get('href')

        if isinstance(model_href, str):
            performer = get_last_url_segment(model_href)
    
    return performer


def main() -> None:
    parser = parse_showcampy()
    args = parser.parse_args(sys.argv[1:])

    for url in args.url:
        logging.info(f'Main URL: {url}')
        base_soup = get_document(url)
        performer = get_performer_name(base_soup)
        all_links = []

        if '/show-cam-sex-movies/' in url and url.endswith('.html'):
            all_links.append(url)
        else:
            logging.info('Fetching performer info')
            page_links, total_pages = get_performer_pages(base_soup)

            for idx, page_link in enumerate(page_links):
                logging.info(f'Fetching links from page {idx+1} out of {total_pages}')

                if page_link == page_links[0]:
                    page_soup = base_soup
                else:
                    page_soup = get_document(page_link)

                links = get_all_page_urls(page_soup)
                all_links.extend(links)

        performer_archive_path  = ARCHIVES_FOLDER / f'{performer}.txt'
        total_all_links = len(all_links)
        touch_archive_path(performer_archive_path)
        archive = read_archive(performer_archive_path)

        for idx, link in enumerate(all_links):
            video_id, video_filename = get_video_filename(performer, link)
            logging.info(f'Video {idx+1} out of {total_all_links}: {video_filename}')
            logging.info(f'Fetching src from: {link}')

            if video_id not in archive:
                video_soup = get_document(link)
                source_website = get_source_website(video_soup)
                actual_video_link = get_actual_video_link(video_soup)

                if actual_video_link:            
                    status_code = test_for_status(actual_video_link)

                    if status_code != 200:
                        logging.error(f'Could not get video link: Status code: {status_code} for {actual_video_link}')
                        continue

                    if source_website:
                            sorted_download_path = DL_PATH / source_website / performer
                    else:
                        sorted_download_path = DL_PATH / performer
                        logging.warning('Source website not found')

                    video_download_path = sorted_download_path / video_filename

                    if isinstance(actual_video_link, str):
                        command = build_command(actual_video_link, video_download_path)
                        subprocess.run(command)

                        if video_download_path.exists():
                            logging.info('Embedding metadata')
                            embed_comment(video_download_path, link)
                            logging.info('Archiving')
                            save_txt(performer_archive_path, f'showcamrips {video_id}\n')
            else:
                logging.info(f'Video already in archive')

        logging.info('Finished downloading playlist')

if __name__ == '__main__':
    main()