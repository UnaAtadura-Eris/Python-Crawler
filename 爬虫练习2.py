import sys
import io
import os
import requests
from bs4 import BeautifulSoup
import pandas as pd
import time

# 解决中文乱码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


# 配置（你只改这里！）
def get_page_url(page_num):
    # 示例格式：return f"https://你的域名/actors?page={page_num}"
    # 你把上面换成你真实的分页网址格式
    return f"https://www.javbus.com/page/{page_num}"  # ← 改这里！

# 防反爬配置
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# 初始化会话（稳定请求）
requests.packages.urllib3.disable_warnings()  # 关闭SSL警告
session = requests.Session()

# 存储结果
all_videos = []

# 爬取单页
def crawl_single_page(page_num):
    url = get_page_url(page_num)
    print(f"\n===== 正在爬第 {page_num} 页：{url} =====")

    try:
        # 发送请求（跳过SSL验证，解决请求失败）
        response = session.get(
            url,
            headers=headers,
            timeout=5,
            verify=False,
            allow_redirects=True
        )
        response.raise_for_status()  # 检测请求是否成功

    except Exception as e:
        print(f"❌ 第{page_num}页请求失败：{str(e)[:200]}")
        return False

    # 解析HTML（精准匹配你给的结构）
    soup = BeautifulSoup(response.text, "html.parser")
   
    video_cards = soup.select("div.item.masonry-brick")

    if not video_cards:
        print("❌ 本页未找到视频卡片")
        return False

    # 提取每个视频的信息
    for card in video_cards:
        try:
        # 1. 视频链接
            a_tag = card.select_one("a.movie-box")
            full_url = a_tag["href"] if a_tag else ""

            # 2. 标题、番号、日期
            photo_info = card.select_one("div.photo-info span")
            if photo_info:
                # 把span里的文本拿出来，去掉多余换行和空格
                text_parts = photo_info.get_text(strip=True, separator="\n").split("\n")
                title = text_parts[0].strip() if text_parts else ""
                
                # 找两个date标签：番号 和 日期
                date_tags = photo_info.select("date")
                code = date_tags[0].get_text(strip=True) if len(date_tags) >= 1 else ""
                publish_date = date_tags[1].get_text(strip=True) if len(date_tags) >= 2 else ""
            else:
                title = ""
                code = ""
                publish_date = ""
            
            # ======================
            # 2. 自动进入视频详情页
            # ======================
            print(f"  → 进入详情页：{full_url}")

            resp_detail = session.get(
                full_url,
                headers=headers,
                timeout=15,
                verify=False
            )
            s = BeautifulSoup(resp_detail.text, "html.parser")

            # ----------------------
            # 第三步：爬你想要的内容（举例，你可以自己加）
            # ----------------------
            # 女优（多个女优自动拼接）
            actress_tags = s.select("span.genre a[href*='/star/']")
            actresses = [a.get_text(strip=True) for a in actress_tags]
            actresses_str = "，".join(actresses)

            # 类别（标签）
            genre_tags = s.select("span.genre a[href*='/genre/']")
            genres = [g.get_text(strip=True) for g in genre_tags]
            genres_str = "，".join(genres)


            all_videos.append({
                "标题": title,
                "链接": full_url,
                "番号": code,
                "发行日期": publish_date,
                "演员": actresses_str,
                "类别": genres_str
            })
            print(f"✅ {title} ")

        except Exception as e:
            print(f"⚠️  解析单张卡片失败：{str(e)}")
            continue

    print(f"✅ 第{page_num}页完成，共抓取 {len(video_cards)} 个视频")
    return True

# 批量爬多页
def crawl_all_pages(start_page=1, end_page=10):
    for page in range(start_page, end_page + 1):
        crawl_single_page(page)
        time.sleep(1)  

# 保存到Excel（自动创建文件夹）
def save_to_excel():
    if not all_videos:
        print("❌ 没有可保存的数据")
        return

    # 自动创建output文件夹
    if not os.path.exists("output"):
        os.mkdir("output")

    # 保存Excel
    df = pd.DataFrame(all_videos)
    excel_path = "output/javbus.xlsx"
    df.to_excel(excel_path, index=False)
    print(f"\n🎉 全部完成！共抓取 {len(all_videos)} 个视频数据")
    print(f"📁 数据已保存到：{os.path.abspath(excel_path)}")

# 主程序入口
if __name__ == "__main__":
    # 你可以改这里的起始页和结束页
    crawl_all_pages(start_page=2, end_page=3)  # 比如爬1-5页
    save_to_excel()



