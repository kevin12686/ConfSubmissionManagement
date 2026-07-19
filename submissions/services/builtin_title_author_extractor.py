import math
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

    doc.close()

    if verify:
        from submissions.services.title_author_verification import generate_verification_image

        generate_verification_image(
            f,
            title,
            author,
            "BUILT-IN",
            verify_folder,
            author_list,
        )

    return title, author, len(author_list)
