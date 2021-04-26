import torch
from PIL import ImageDraw, ImageFont
from tqdm import tqdm

from constants import INFERENCE_PATH, CLASS_DICT
from src.models.helper_functions import filter_by_nms, filter_by_score


def view_test_images(model_file_name, data_loader, iou_threshold):
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

    pbar = tqdm(total=len(data_loader), leave=False, desc='Inference Test')

    model = torch.load(f'models/{model_file_name}')
    model.eval()

    font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Regular.ttf", 12)

    # Deactivate the autograd engine
    with torch.no_grad():
        for batch_no, (images, targets) in enumerate(data_loader):
            indices = range(data_loader.batch_size * batch_no,
                            data_loader.batch_size * (batch_no + 1))

            images = torch.stack(images).to(device)

            preds = model(images)
            preds = filter_by_nms(preds, iou_threshold)
            preds = filter_by_score(preds, iou_threshold)

            try:
                images = [data_loader.dataset.load_image(index) for index in indices]

            except IndexError:
                continue

            for index, image, target, pred in zip(indices, images, targets, preds):
                draw = ImageDraw.Draw(image)

                draw_boxes(draw, target, 'green', 3, font, (255, 255, 0, 0))
                draw_boxes(draw, pred, 'red', 3, font, (255, 255, 255, 0))

                image.save(f'{INFERENCE_PATH}'
                           f'{data_loader.dataset.image_paths[index].rsplit("/", 1)[-1].split(".", 1)[0]}.png')



            pbar.update()

    pbar.close()


def draw_boxes(draw, box_dict, outline_color, outline_width, font, font_color):
    boxes = box_dict['boxes'].tolist()
    labels = [list(CLASS_DICT.keys())[list(CLASS_DICT.values()).index(label)]
              for label in box_dict['labels'].tolist()]

    if 'scores' in box_dict:
        scores = box_dict['scores'].tolist()
    else:
        scores = [1] * len(labels)

    for box, label, score in zip(boxes, labels, scores):
        draw.rectangle(box, outline=outline_color, width=outline_width)
        draw.text((box[0], box[1]), text=f'{label} {score:.4f}', font=font, fill=font_color)