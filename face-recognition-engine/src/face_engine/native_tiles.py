"""Bounded native-image face detection; never manufacture facial detail."""
import numpy as np
import cv2


def detect_native_tiles(detector, image):
    height,width=image.shape[:2]
    if max(height,width)<=960:return []
    tile_w=min(width,max(640,int(np.ceil(width*.4))))
    tile_h=min(height,max(640,int(np.ceil(height*.6))))
    xs=sorted(set(int(round(v)) for v in np.linspace(0,width-tile_w,3)))
    ys=sorted(set(int(round(v)) for v in np.linspace(0,height-tile_h,2)))
    detect=getattr(detector,'standard',detector).detect_faces;found=[]
    for y in ys:
        for x in xs:
            for face in detect(image[y:y+tile_h,x:x+tile_w]):
                box=face['bbox']
                # Internal tile cuts must not turn a face fragment into a
                # second person. Overlap gives that face another complete tile.
                if ((x>0 and box[0]<=1) or (y>0 and box[1]<=1) or
                    (x+tile_w<width and box[2]>=tile_w-1) or
                    (y+tile_h<height and box[3]>=tile_h-1)):continue
                shifted=dict(face)
                for key in ['bbox','raw_bbox']:
                    if key in face:shifted[key]=(np.asarray(face[key],dtype=float)+[x,y,x,y]).tolist()
                shifted['landmarks']=(np.asarray(face['landmarks'],dtype=float)+[x,y]).tolist()
                found.append(shifted)
    result=[]
    for face in sorted(found,key=lambda f:f['score'],reverse=True):
        a=np.asarray(face['bbox'],dtype=float);duplicate=False
        for old in result:
            b=np.asarray(old['bbox'],dtype=float);area=np.maximum(0,np.minimum(a[2:],b[2:])-np.maximum(a[:2],b[:2])).prod()
            union=np.maximum(0,a[2:]-a[:2]).prod()+np.maximum(0,b[2:]-b[:2]).prod()-area
            if union>0 and area/union>.4:duplicate=True;break
        if not duplicate:result.append(face)
    return result


def motion_region_faces(detector,image,previous_gray):
    """Find extra faces in motion crops of THIS native main frame only."""
    if previous_gray is None:return detect_native_tiles(detector,image)
    height,width=image.shape[:2];small=cv2.resize(image,(320,180));gray=cv2.cvtColor(small,cv2.COLOR_BGR2GRAY)
    mask=(cv2.absdiff(gray,previous_gray)>16).astype(np.uint8)*255
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((2,2),np.uint8))
    count,_,stats,_=cv2.connectedComponentsWithStats(mask,8);regions=[]
    for i in range(1,count):
        x,y,w,h,area=map(int,stats[i])
        if area<8 or area>320*180*.5:continue
        cx=(x+w/2)*width/320;cy=(y+h/2)*height/180
        cw=min(width,max(640,int(w*width/320*1.5)));ch=min(height,max(640,int(h*height/180*1.5)))
        left=max(0,min(width-cw,int(cx-cw/2)));top=max(0,min(height-ch,int(cy-ch/2)))
        region=(left,top,left+cw,top+ch)
        regions.append((area,region))
    selected=[]
    for _,region in sorted(regions,reverse=True):
        a=np.asarray(region)
        duplicate=False
        for old in selected:
            b=np.asarray(old);inter=np.maximum(0,np.minimum(a[2:],b[2:])-np.maximum(a[:2],b[:2])).prod()
            union=np.maximum(0,a[2:]-a[:2]).prod()+np.maximum(0,b[2:]-b[:2]).prod()-inter
            if union and inter/union>.7:duplicate=True;break
        if not duplicate:selected.append(region)
    found=[];detect=getattr(detector,'standard',detector).detect_faces
    for left,top,right,bottom in selected[:6]:
        for face in detect(image[top:bottom,left:right]):
            box=face['bbox']
            if ((left>0 and box[0]<=1) or (top>0 and box[1]<=1) or
                (right<width and box[2]>=right-left-1) or (bottom<height and box[3]>=bottom-top-1)):continue
            shifted=dict(face)
            for key in ['bbox','raw_bbox']:
                if key in face:shifted[key]=(np.asarray(face[key],dtype=float)+[left,top,left,top]).tolist()
            shifted['landmarks']=(np.asarray(face['landmarks'],dtype=float)+[left,top]).tolist();found.append(shifted)
    result=[]
    for face in sorted(found,key=lambda f:f['score'],reverse=True):
        a=np.asarray(face['bbox']);duplicate=False
        for old in result:
            b=np.asarray(old['bbox']);inter=np.maximum(0,np.minimum(a[2:],b[2:])-np.maximum(a[:2],b[:2])).prod()
            union=np.maximum(0,a[2:]-a[:2]).prod()+np.maximum(0,b[2:]-b[:2]).prod()-inter
            if union>0 and inter/union>.4:duplicate=True;break
        if not duplicate:result.append(face)
    return result
