# -*- coding: utf-8 -*-
"""把 Image/idle.png 转换成 Windows 图标 (.ico)

## 为什么需要转换

PyInstaller 的 `--icon` 只接受 `.ico`（或 .exe 资源），不直接吃 PNG。

## 为什么要"等比缩放 + 居中留白"

idle.png 是 419x725 的**竖版**立绘。若直接压成正方形，人物会被压扁，
图标会明显走形。这里保持宽高比，把高度撑满正方形，左右用透明补齐 ——
横向会留出约 43% 的透明边，但人物比例正常。

## 为什么需要"额外收紧"

立绘四角往往是接近透明但非零的像素（抗锯齿残留、极淡的阴影）。
这类像素会让 Windows 在浅色背景（文件资源管理器白色列表）上
显出灰蒙蒙的方块。这里把它们压成全透明，只保留真正的人物。

生成的尺寸覆盖 Windows 常用档位，保证在 16x16 的小图标视图下
和 256x256 的大图标视图下都有合适的位图可用。
"""
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "Image", "idle.png")
OUT = os.path.join(ROOT, "app.ico")

# Windows 图标常用尺寸
SIZES = [256, 128, 64, 48, 32, 24, 16]

# 视为"背景噪声"的 alpha 阈值：低于此值的像素直接设为全透明
ALPHA_FLOOR = 12


def build():
    if not os.path.isfile(SRC):
        print(f"找不到源图: {SRC}")
        return 1

    src = Image.open(SRC).convert("RGBA")
    print(f"源图      : {SRC}")
    print(f"原始尺寸  : {src.size[0]}x{src.size[1]}")

    # ① 清理背景噪声：把极低 alpha 的像素压成完全透明
    alpha = src.getchannel("A")
    cleaned = alpha.point(lambda v: 0 if v < ALPHA_FLOOR else v)
    src.putalpha(cleaned)

    bbox = cleaned.getbbox()
    print(f"有效内容区: {bbox}")
    if bbox:
        src = src.crop(bbox)
        print(f"裁剪后尺寸: {src.width}x{src.height}")
    else:
        print("警告：整张图都是透明像素，请检查源图")

    # ② 放到正方形画布：高度撑满，左右留透明
    side = max(src.width, src.height)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    offset = ((side - src.width) // 2, (side - src.height) // 2)
    canvas.paste(src, offset, src)
    print(f"方形画布  : {side}x{side}（左右透明留白 "
          f"{(side - src.width) // 2}px）")

    # ③ 生成各档尺寸并保存为多尺寸 ico
    frames = []
    for size in SIZES:
        frames.append(canvas.resize((size, size), Image.LANCZOS))
    frames[0].save(OUT, format="ICO",
                   sizes=[(s, s) for s in SIZES],
                   append_images=frames[1:])

    print()
    print(f"已生成    : {OUT}")
    print(f"文件大小  : {os.path.getsize(OUT) / 1024:.1f} KB")
    print(f"包含尺寸  : {', '.join(f'{s}x{s}' for s in SIZES)}")

    # 回读校验
    check = Image.open(OUT)
    print(f"回读校验  : format={check.format} size={check.size}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
