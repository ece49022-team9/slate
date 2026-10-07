import argparse
import asyncio

from slate.board import ROOT
from slate.link import open_board
from slate.voice.device import cloud, socket_url


async def provision(port: str, ssid: str, password: str) -> None:
    url, token = cloud()
    link, reader = await open_board(port, ROOT / ".local/bench-serial.log")
    try:
        online = asyncio.ensure_future(link.wait_for("slate.net: up", 30))
        link.type(f"!net wifi {ssid} {password}\n")
        print(f"slate.provision: {(await online).strip()}")
        connected = asyncio.ensure_future(link.wait_for("slate.cloud: connected", 30))
        link.type(f"!cloud {socket_url(url)} {token}\n")
        await connected
        print(f"slate.provision: board connected to {url}")
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port")
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()
    if " " in args.ssid or " " in args.password:
        parser.error("Wi-Fi names and passwords with spaces are not supported yet")
    asyncio.run(provision(args.port, args.ssid, args.password))


if __name__ == "__main__":
    main()
