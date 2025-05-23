import argparse
import torch
import gradio as gr

from llava.constants import IMAGE_TOKEN_INDEX, DEFAULT_IMAGE_TOKEN, DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN
from llava.conversation import conv_templates, SeparatorStyle
from llava.model.builder import load_pretrained_model
from llava.utils import disable_torch_init
from llava.mm_utils import process_images, tokenizer_image_token, get_model_name_from_path

from PIL import Image
from io import BytesIO
from transformers import TextStreamer
import requests


def load_image(image_file):
    if image_file.startswith('http://') or image_file.startswith('https://'):
        response = requests.get(image_file)
        image = Image.open(BytesIO(response.content)).convert('RGB')
    else:
        image = Image.open(image_file).convert('RGB')
    return image


def generate(image, user_prompt):
    # Initialize a new conversation state for each call
    conv = conv_templates[conv_mode].copy()
    roles = ('user', 'assistant') if "mpt" in model_name.lower() else conv.roles

    if image is not None:
        image_size = image.size
        image_tensor = process_images([image], image_processor, model.config)
        if type(image_tensor) is list:
            image_tensor = [image.to(model.device, dtype=torch.float16) for image in image_tensor]
        else:
            image_tensor = image_tensor.to(model.device, dtype=torch.float16)

        if model.config.mm_use_im_start_end:
            user_prompt = DEFAULT_IM_START_TOKEN + DEFAULT_IMAGE_TOKEN + DEFAULT_IM_END_TOKEN + '\n' + user_prompt
        else:
            user_prompt = DEFAULT_IMAGE_TOKEN + '\n' + user_prompt

    # print(model.config.image_aspect_ratio)
    conv.append_message(conv.roles[0], user_prompt)
    conv.append_message(conv.roles[1], None)
    prompt = conv.get_prompt()
    print("<1>"+prompt+"<2>")

    input_ids = tokenizer_image_token(prompt, tokenizer, IMAGE_TOKEN_INDEX, return_tensors='pt').unsqueeze(0).to(model.device)
    stop_str = conv.sep if conv.sep_style != SeparatorStyle.TWO else conv.sep2
    streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)

    with torch.inference_mode():
        output_ids = model.generate(
            input_ids,
            images=image_tensor,
            image_sizes=[image_size],
            do_sample=True if args.temperature > 0 else False,
            temperature=args.temperature,
            max_new_tokens=args.max_new_tokens,
            streamer=streamer,
            use_cache=True,
            output_hidden_states=True,
            return_dict_in_generate=True
        )

    # outputs = tokenizer.decode(output_ids[0]).strip()
    outputs = tokenizer.decode(output_ids['sequences'][0], skip_special_tokens=True)
    conv.messages[-1][-1] = outputs

    # inputs... <s> Yes </s>
    #            |   |   |
    #            ø   F   L
    # ø: Dummy token by LLaVA, not actually generated (with hidden states)
    # F: First generated token (with hidden states)
    # L: Last generated token (no hidden states)
    layer_id = -1
    input_embeddings = output_ids["hidden_states"][0][layer_id] # 1, input_seqlen, hidden_size
    output_embeddings = [hidden_state[layer_id] for hidden_state in output_ids["hidden_states"][1:]] # output_seqlen * [1, 1, hidden_size]
    output_embeddings = torch.stack(output_embeddings, dim=2).squeeze(0) # 1, output_seqlen, hidden_size
    all_embeddings = torch.cat([input_embeddings, output_embeddings], dim=1)

    output_rep = output_embeddings.cpu().detach().numpy().mean(axis=1)
    all_rep = all_embeddings.cpu().detach().numpy().mean(axis=1)

    return output_rep, all_rep, outputs

def gradio_interface():
    return generate


parser = argparse.ArgumentParser()
parser.add_argument("--model-path", type=str, default="")
parser.add_argument("--model-base", type=str, default=None)
parser.add_argument("--device", type=str, default="cuda")
parser.add_argument("--conv-mode", type=str, default=None)
parser.add_argument("--temperature", type=float, default=0.2)
parser.add_argument("--max-new-tokens", type=int, default=512)
parser.add_argument("--load-8bit", action="store_true")
parser.add_argument("--load-4bit", action="store_true")
parser.add_argument("--debug", action="store_true")
parser.add_argument("--port", type=int, default=7880)
args = parser.parse_args()

# Model Initialization
disable_torch_init()
model_name = get_model_name_from_path(args.model_path)
tokenizer, model, image_processor, context_len = load_pretrained_model(args.model_path, args.model_base, model_name, args.load_8bit, args.load_4bit, device=args.device)

if "llama-2" in model_name.lower():
    conv_mode = "llava_llama_2"
elif "mistral" in model_name.lower():
    conv_mode = "mistral_instruct"
elif "v1.6-34b" in model_name.lower():
    conv_mode = "chatml_direct"
elif "v1" in model_name.lower():
    conv_mode = "llava_v1"
elif "mpt" in model_name.lower():
    conv_mode = "mpt"
else:
    conv_mode = "llava_v0"

if args.conv_mode is not None and conv_mode != args.conv_mode:
    conv_mode = args.conv_mode

demo = gr.Interface(
    fn=gradio_interface(),
    inputs=[gr.Image(type="pil"), gr.Textbox(label="user_prompt")],
    outputs=[gr.Dataframe(type="numpy"), gr.Dataframe(type="numpy"), gr.Textbox(label="Generated Text")]
)

demo.queue().launch(server_port=args.port, share=True)
