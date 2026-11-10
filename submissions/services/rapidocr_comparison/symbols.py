"""Explicit direct visual substitutions, never spelling correction or a skeleton."""
from itertools import combinations

# Font-dependent research choices, not a universal assertion of equal glyphs.
GROUPS=[
 ('round','Round forms',('0','O','o','Ο','ο','О','о'),'core','Genuine O/0, case or cross-script substitutions may be tolerated.'),
 ('vertical','Vertical strokes',('1','I','l','|','І','Ӏ'),'core','Genuine changes between digits, letters and absolute-value bars may be tolerated.'),
 ('dash','Dash forms',('-','‐','‑','–','—','−'),'core','The stroke must remain; deletion of a minus sign is not tolerated.'),
 ('single_quote','Single quote forms',("'",'‘','’','ʼ'),'core','A quotation mark must remain; insertion or deletion is not tolerated.'),
 ('double_quote','Double quote forms',('"','“','”'),'core','A quotation mark must remain; insertion or deletion is not tolerated.'),
 ('plus','Plus forms',('+','＋','十'),'core','A real change between a mathematical plus and the Han character ten may be tolerated.'),
 ('times','Cross forms',('x','×','✕'),'core','A real change between variable x and multiplication signs may be tolerated.'),
 ('middle_dot','Middle dot forms',('·','⋅','∙'),'core','Excludes decimal points; deletion of a dot is not tolerated.'),
 ('fi','fi ligature',('fi','ﬁ'),'core','Only the listed ligature forms are tolerated, not general compatibility normalization.'),
 ('fl','fl ligature',('fl','ﬂ'),'core','Genuine changes between the listed ligature forms may be tolerated.'),
 ('ff','ff ligature',('ff','ﬀ'),'core','Genuine changes between the listed ligature forms may be tolerated.'),
 ('ffi','ffi ligature',('ffi','ﬃ'),'core','Genuine changes between the listed ligature forms may be tolerated.'),
 ('ffl','ffl ligature',('ffl','ﬄ'),'core','Genuine changes between the listed ligature forms may be tolerated.'),
 ('rn_m','rn / m',('rn','m'),'core','Genuine rn/m spelling changes may be tolerated.'),
 ('cl_d','cl / d',('cl','d'),'core','Genuine cl/d spelling changes may be tolerated.'),
 ('vv_w','vv / w',('vv','w'),'core','Genuine vv/w spelling changes may be tolerated.'),
 ('A','Similar A forms',('A','Α','А'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('a','Similar a forms',('a','а'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('B','Similar B forms',('B','Β','В'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('C','Similar C forms',('C','С'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('c','Similar c forms',('c','с'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('E','Similar E forms',('E','Ε','Е'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('e','Similar e forms',('e','е'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('H','Similar H forms',('H','Η','Н'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('K','Similar K forms',('K','Κ','К'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('M','Similar M forms',('M','Μ','М'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('N','Similar N forms',('N','Ν'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('P','Similar P forms',('P','Ρ','Р'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('p','Similar p forms',('p','ρ','р'),'core','Genuine changes between Greek rho and p may be tolerated.'),
 ('T','Similar T forms',('T','Τ','Т'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('X','Similar X forms',('X','Χ','Х'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('Y','Similar Y forms',('Y','Υ','Ү'),'core','Genuine cross-script character substitutions may be tolerated.'),
 ('two_Z','2 / Z',('2','Z'),'broad','Font-dependent similarity; genuine digit/letter substitutions may be tolerated.'),
 ('five_S','5 / S',('5','S'),'broad','Font-dependent similarity; genuine digit/letter substitutions may be tolerated.'),
 ('eight_B','8 / B',('8','B'),'broad','Font-dependent similarity; genuine digit/letter substitutions may be tolerated.'),
 ('six_G','6 / G',('6','G'),'broad','Font-dependent similarity; genuine digit/letter substitutions may be tolerated.'),
 ('f_t','f / t',('f','t'),'broad','Tolerates safety/satety, including genuine f/t changes in the PDF.'),
 ('u_v','u / v',('u','v'),'broad','Font-dependent similarity; genuine variable or spelling changes may be tolerated.')]

PROFILES={'literal':set(),'core':{'core'},'broad':{'core','broad'}}


def rules(profile):
    out=[]
    for rid,label,forms,tier,risk in GROUPS:
        if tier not in PROFILES[profile]:continue
        for a,b in combinations(forms,2):
            out.extend([(a,b,rid),(b,a,rid)])
    return out


def table():
    return [{'id':i,'label':l,'forms':list(f),'tier':t,'risk':r,
             'codepoints':[[f'U+{ord(c):04X}' for c in v] for v in f]} for i,l,f,t,r in GROUPS]
