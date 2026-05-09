import math
import os
import re

import pymupdf


def flags_decomposer(flags):
    """Make font flags human readable."""
    l = []
    if flags & 2**0:
        l.append("superscript")
    if flags & 2**1:
        l.append("italic")
    if flags & 2**2:
        l.append("serifed")
    else:
        l.append("sans")
    if flags & 2**3:
        l.append("monospaced")
    else:
        l.append("proportional")
    if flags & 2**4:
        l.append("bold")
    return l


def get_title_author(f, verify=True, verify_folder="verification"):
    doc = pymupdf.open(f)
    page = doc[0]
    blocks = page.get_text("dict", sort=True)["blocks"]

    title_flag = False  # flag of process title
    title_size = 0  # title font size
    title = ""  # result of title

    # Title in lines
    title_lines = list()

    author_flag = False  # flag of process author
    author = ""  # result of author

    exit_flag = False  # exit flag

    for b in blocks:  # iterate through the text blocks

        # exit signal
        if exit_flag:
            break

        for l in b["lines"]:  # iterate through the text lines

            buffer = ""  # buffer for line reading
            size = l["spans"][0]["size"]  # font size of first spans
            styles = flags_decomposer(l["spans"][0]["flags"])  # styles of first span

            # condition of starting title process
            if not title_flag and (size > 13.5 or "bold" in styles):
                title_flag = True
                title_size = size

            # condition of ending title process and starting author process
            elif title_flag and math.fabs(title_size - size) > 1:
                title_flag = False
                author_flag = True

            for s in l["spans"]:  # iterate through the text spans

                # processing title
                if title_flag:
                    buffer += s["text"]
                    title += s["text"]

                # processing author
                if author_flag:
                    flags = flags_decomposer(s["flags"])
                    if "superscript" in flags:  # put a dummy # if it is superscript
                        buffer += "#"
                    else:
                        buffer += s["text"]

            # is processing title
            if title_flag:
                title += " "  # avoid newline without space
                title_lines.append(re.sub(r"\s+", r" ", buffer.strip()))

            # is processing author
            if author_flag:
                if not author:  # first time of processing author
                    if not buffer.strip():  # skip empty line
                        continue
                    author += buffer.replace("#", " ")  # replace dummy # with space
                else:
                    # check if 2+ lines of author
                    if (
                        not (
                            re.match(r"^[#\d]", buffer.strip())
                            or buffer.lower()
                            .strip()
                            .startswith(
                                (
                                    "university ",
                                    "department ",
                                    "school ",
                                    "college ",
                                    "institute ",
                                )
                            )
                        )
                        and (
                            re.match(r".+\W(\s*and)?$", author.strip())
                            or buffer.strip().startswith((",", "and "))
                        )
                    ):
                        author += " " + buffer.replace("#", " ")
                    else:
                        exit_flag = True  # finish process, exit singal
                        break

    # general cleaning up and formating
    title = re.sub(r"\s+", r" ", title.strip())
    author = re.sub(r"\s+", r" ", author.strip().replace("*", ""))
    author = re.sub(r"\s*,\s*", r", ", author)
    author = re.sub(r"(.+)(?<!,)(, (and\s+)?|\s+and\s+)(.+)", r"\1, and \4", author)

    # special case: only 2 authors
    if author.count(",") <= 1:
        author = author.replace(",", "")

    # remove redundant space
    title = re.sub(r"\s+", " ", title.strip())
    author = re.sub(r"\s+", " ", author.strip())

    # remove the "," at the end
    if author.endswith(","):
        author = author[:-1]

    # extract author names for verify
    author_list = author.replace(", and ", ", ").replace(" and ", ", ").split(", ")

    if verify:

        # only show the first 1/3 of the page
        crop_r = pymupdf.Rect(0, 0, doc[0].rect.x1, doc[0].rect.y1 / 3)

        # title highlight
        for each in title_lines:
            r = page.search_for(each, clip=crop_r, quads=True)
            if r:
                anot = page.add_highlight_annot(r[0])
                anot = page.add_underline_annot(r[0])
                anot.set_colors(stroke=(0, 0, 1))
                anot.update()

        # author highlight
        for each in author_list:
            r = page.search_for(each, clip=crop_r, quads=True)
            if r:
                anot = page.add_highlight_annot(r[0])
                anot.set_colors(stroke=(0, 1, 0))
                anot.update()
                anot = page.add_squiggly_annot(r[0])

        # location of title & author print
        r = page.search_for(title_lines[0], clip=crop_r, quads=True)
        if r and r[0].rect.x1 - r[0].rect.x0 > 300:
            r = r[0].rect + pymupdf.Rect(0, -40, 0, -15)
            if r.x0 < 70:
                r.x0 = 70
        else:
            r = pymupdf.Rect(70, 20, 570, 40)

        # print title
        page.add_freetext_annot(
            r,
            title,
            fontsize=9,
            text_color=(0, 0, 1),
            align=pymupdf.TEXT_ALIGN_CENTER,
        )

        # print author
        page.add_freetext_annot(
            r + pymupdf.Rect(0, 22, 0, 22),
            author,
            fontsize=9,
            text_color=(1, 0, 0),
            align=pymupdf.TEXT_ALIGN_CENTER,
        )

        # stamp and filename
        page.add_freetext_annot(
            pymupdf.Rect(15, 30, 70, 45),
            os.path.basename(f),
            fontsize=7,
            text_color=(0, 0, 0),
            align=pymupdf.TEXT_ALIGN_CENTER,
        )
        annot = page.add_stamp_annot(pymupdf.Rect(15, 10, 70, 30), stamp=7)  # stamp
        annot.set_colors(stroke=(1, 0, 0))
        annot.update()

        # crop the page
        doc[0].set_cropbox(crop_r)
        # save as image
        doc[0].get_pixmap(dpi=300).save(os.path.join(verify_folder, os.path.basename(f)) + ".png")

    return title, author, len(author_list)
