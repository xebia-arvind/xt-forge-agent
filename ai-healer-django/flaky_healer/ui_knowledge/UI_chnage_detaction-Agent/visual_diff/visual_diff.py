import cv2
import numpy as np
from skimage.metrics import structural_similarity as ssim

PIXEL_DIFF_THRESHOLD = 0.01   # 1%
SSIM_THRESHOLD = 0.95         # <95% similarity = change

def load_image(path):
    return cv2.imread(path)

def apply_mask(image, mask_path=None):
    if mask_path is None:
        return image
    mask = cv2.imread(mask_path, 0)
    return cv2.bitwise_and(image, image, mask=mask)

def pixel_diff(img1, img2):
    diff = cv2.absdiff(img1, img2)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    non_zero = np.count_nonzero(gray)
    total = gray.size
    ratio = non_zero / total
    return ratio, diff

def ssim_diff(img1, img2):
    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
    score, diff = ssim(gray1, gray2, full=True)
    diff = (diff * 255).astype("uint8")
    return score, diff

def highlight_changes(base, diff):
    thresh = cv2.threshold(
        cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY),
        30, 255, cv2.THRESH_BINARY
    )[1]
    contours, _ = cv2.findContours(
        thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    output = base.copy()
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h > 100:  # ignore tiny noise
            cv2.rectangle(output, (x, y), (x+w, y+h), (0, 0, 255), 2)
    return output

def resize_to_match(img1, img2):
    h = min(img1.shape[0], img2.shape[0])
    w = min(img1.shape[1], img2.shape[1])
    img1 = img1[:h, :w]
    img2 = img2[:h, :w]
    return img1, img2


def visual_diff(
    baseline_path,
    current_path,
    mask_path=None,
    output_path="visual_diff_result.png"
):
    base = load_image(baseline_path)
    curr = load_image(current_path)

    base = apply_mask(base, mask_path)
    curr = apply_mask(curr, mask_path)

    base, curr = resize_to_match(base, curr)
    pixel_ratio, pixel_diff_img = pixel_diff(base, curr)
    ssim_score, ssim_diff_img = ssim_diff(base, curr)

    visual_change = (
        pixel_ratio > PIXEL_DIFF_THRESHOLD or
        ssim_score < SSIM_THRESHOLD
    )

    highlighted = highlight_changes(curr, pixel_diff_img)
    cv2.imwrite(output_path, highlighted)

    return {
        "visual_change": visual_change,
        "pixel_change_ratio": round(pixel_ratio, 4),
        "ssim_score": round(ssim_score, 4),
        "diff_image": output_path
    }
