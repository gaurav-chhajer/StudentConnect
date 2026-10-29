"""Portable XLSX export for the local Python worker, without new dependencies."""

from io import BytesIO
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile, ZIP_DEFLATED

MAIN='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE='http://schemas.openxmlformats.org/package/2006/relationships'
CONTENT='http://schemas.openxmlformats.org/package/2006/content-types'


def xml(element):
    return tostring(element,encoding='utf-8',xml_declaration=True)


def column(number):
    result=''
    while number:
        number,remainder=divmod(number-1,26)
        result=chr(65+remainder)+result
    return result


def workbook(sheets):
    """All external strings are inline text, so spreadsheet formulas cannot execute."""
    if not 1<=len(sheets)<=8:
        raise ValueError('Invalid workbook sheet count')
    output=BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        content=Element('Types',xmlns=CONTENT)
        SubElement(content,'Default',Extension='rels',ContentType='application/vnd.openxmlformats-package.relationships+xml')
        SubElement(content,'Default',Extension='xml',ContentType='application/xml')
        for name,kind in (('/xl/workbook.xml','sheet.main'),('/xl/styles.xml','styles')):
            SubElement(content,'Override',PartName=name,ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.'+kind+'+xml')
        root_rels=Element('Relationships',xmlns=PACKAGE)
        SubElement(root_rels,'Relationship',Id='rId1',Type=REL+'/officeDocument',Target='xl/workbook.xml')
        archive.writestr('_rels/.rels',xml(root_rels))
        book=Element('workbook',xmlns=MAIN)
        names=SubElement(book,'sheets')
        relationships=Element('Relationships',xmlns=PACKAGE)
        for index,(name,rows) in enumerate(sheets,1):
            if not rows or len(rows)>10002 or any(len(row)>30 for row in rows):
                raise ValueError('Workbook rows exceed supported limits')
            if not name or len(name)>31 or any(c in name for c in '[]:*?/\\'):
                raise ValueError('Invalid sheet name')
            SubElement(names,'sheet',name=name,sheetId=str(index),attrib={'{'+REL+'}id':'rId'+str(index)})
            SubElement(relationships,'Relationship',Id='rId'+str(index),Type=REL+'/worksheet',Target='worksheets/sheet'+str(index)+'.xml')
            SubElement(content,'Override',PartName='/xl/worksheets/sheet'+str(index)+'.xml',
                ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            sheet=Element('worksheet',xmlns=MAIN)
            views=SubElement(sheet,'sheetViews')
            view=SubElement(views,'sheetView',workbookViewId='0')
            SubElement(view,'pane',ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen')
            cols=SubElement(sheet,'cols')
            for number in range(1,len(rows[0])+1):
                SubElement(cols,'col',min=str(number),max=str(number),width='8' if number==1 else '38',customWidth='1')
            data=SubElement(sheet,'sheetData')
            links=[]
            for row_number,values in enumerate(rows,1):
                row=SubElement(data,'row',r=str(row_number),ht='32' if row_number==1 else '70',customHeight='1')
                for col_number,value in enumerate(values,1):
                    cell=SubElement(row,'c',r=column(col_number)+str(row_number),s='1' if row_number==1 else '0')
                    if type(value) in (int,float):
                        SubElement(cell,'v').text=str(value)
                    else:
                        cell.set('t','inlineStr')
                        rendered='' if value is None else str(value)
                        if len(rendered)>32767:
                            raise ValueError('Spreadsheet cell exceeds Excel text limit')
                        if any((ord(c)<32 and c not in '\n\r\t') or 0xD800<=ord(c)<=0xDFFF or ord(c) in (0xFFFE,0xFFFF) for c in rendered):
                            raise ValueError('Spreadsheet text contains invalid XML characters')
                        SubElement(SubElement(cell,'is'),'t',attrib={'{http://www.w3.org/XML/1998/namespace}space':'preserve'}).text=rendered
                        if rendered.startswith('https://') and len(rendered)<=2000 and not any(ord(c)<33 for c in rendered):
                            links.append((column(col_number)+str(row_number),rendered))
            SubElement(sheet,'autoFilter',ref='A1:'+column(len(rows[0]))+str(len(rows)))
            if links:
                hyperlinks=SubElement(sheet,'hyperlinks')
                link_rels=Element('Relationships',xmlns=PACKAGE)
                for link_index,(reference,url) in enumerate(links,1):
                    relation='rId'+str(link_index)
                    SubElement(hyperlinks,'hyperlink',ref=reference,attrib={'{'+REL+'}id':relation})
                    SubElement(link_rels,'Relationship',Id=relation,Type=REL+'/hyperlink',Target=url,TargetMode='External')
                archive.writestr('xl/worksheets/_rels/sheet'+str(index)+'.xml.rels',xml(link_rels))
            archive.writestr('xl/worksheets/sheet'+str(index)+'.xml',xml(sheet))
        SubElement(relationships,'Relationship',Id='rIdStyles',Type=REL+'/styles',Target='styles.xml')
        styles=Element('styleSheet',xmlns=MAIN)
        fonts=SubElement(styles,'fonts',count='2')
        for bold,color in ((False,'FF172B4D'),(True,'FFFFFFFF')):
            font=SubElement(fonts,'font')
            SubElement(font,'sz',val='11')
            SubElement(font,'name',val='Calibri')
            SubElement(font,'color',rgb=color)
            if bold:
                SubElement(font,'b')
        fills=SubElement(styles,'fills',count='3')
        for kind in ('none','gray125','solid'):
            fill=SubElement(fills,'fill')
            pattern=SubElement(fill,'patternFill',patternType=kind)
            if kind=='solid':
                SubElement(pattern,'fgColor',rgb='FF17365D')
                SubElement(pattern,'bgColor',indexed='64')
        borders=SubElement(styles,'borders',count='1')
        SubElement(borders,'border')
        base=SubElement(styles,'cellStyleXfs',count='1')
        SubElement(base,'xf',numFmtId='0',fontId='0',fillId='0',borderId='0')
        formats=SubElement(styles,'cellXfs',count='2')
        for index in range(2):
            item=SubElement(formats,'xf',numFmtId='0',fontId=str(index),fillId='2' if index else '0',borderId='0',xfId='0',applyAlignment='1')
            SubElement(item,'alignment',vertical='top',wrapText='1')
        cell_styles=SubElement(styles,'cellStyles',count='1')
        SubElement(cell_styles,'cellStyle',name='Normal',xfId='0',builtinId='0')
        archive.writestr('xl/styles.xml',xml(styles))
        archive.writestr('xl/workbook.xml',xml(book))
        archive.writestr('xl/_rels/workbook.xml.rels',xml(relationships))
        archive.writestr('[Content_Types].xml',xml(content))
    return output.getvalue()
