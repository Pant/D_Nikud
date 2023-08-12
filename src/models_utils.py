# ML
# DL
import os
import random

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
# visual
import seaborn as sns
import torch
from sklearn.metrics import classification_report
from sklearn.metrics import confusion_matrix
from tqdm import tqdm

from src.running_params import DEBUG_MODE
from src.utiles_data import Nikud


def save_model(model, path):
    model_state = model.state_dict()
    torch.save(model_state, path)


def load_model(model, path):
    checkpoint = torch.load(path)
    model.load_state_dict(checkpoint)
    return model


def get_model_parameters(params, logger, num_freeze_layers=2):
    top_layer_params = []
    for name, param in params:
        requires_grad = True
        for i in range(num_freeze_layers):
            if f'layer.{i}.' in name or "embeddings" in name:
                param.requires_grad = False
                msg = f"{name} : requires_grad = False, shape: {list(param.data.shape)}"
                logger.debug(msg)
                requires_grad = False
                break

        if requires_grad:
            top_layer_params.append(param)
            param.requires_grad = True
            msg = f"{name} : requires_grad = True, shape: {list(param.data.shape)}"
            logger.debug(msg)

    return top_layer_params


def freeze_model_parameters(params):
    top_layer_params = []
    for name, param in params:
        param.requires_grad = False

    return top_layer_params


def find_num_correct_words(input, letter_correct_mask):
    input[np.where(input == 104)[0]] = 0
    input[np.where(input == 1)[0]] = 0
    input[np.where(input == 2)[0]] = 0
    words_end_index = np.concatenate((np.array([-1]), np.where(input == 0)[0]))
    is_correct_words_array = [
        bool(letter_correct_mask[list(range((words_end_index[s] + 1), words_end_index[s + 1]))].all()) for s
        in range(len(words_end_index) - 1) if words_end_index[s + 1] - (words_end_index[s] + 1) > 1]
    return np.array(is_correct_words_array).sum(), len(is_correct_words_array)


def predict(model, data_loader):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    all_labels = None
    with torch.no_grad():
        for index_data, data in enumerate(data_loader):
            (inputs, attention_mask, labels_demo) = data
            inputs = inputs.to(device)
            attention_mask = attention_mask.to(device)
            labels_demo = labels_demo.to(device)

            mask_nikud = np.array(labels_demo.cpu())[:, :, 0] == -1
            mask_dagesh = np.array(labels_demo.cpu())[:, :, 1] == -1
            mask_sin = np.array(labels_demo.cpu())[:, :, 2] == -1

            nikud_probs, dagesh_probs, sin_probs = model(inputs, attention_mask)

            pred_nikud = np.array(torch.max(nikud_probs, 2).indices.cpu()).reshape(inputs.shape[0],
                                                                       inputs.shape[1], 1)
            pred_dagesh = np.array(torch.max(dagesh_probs, 2).indices.cpu()).reshape(inputs.shape[0],
                                                                       inputs.shape[1], 1)
            pred_sin = np.array(torch.max(sin_probs, 2).indices.cpu()).reshape(inputs.shape[0],
                                                                    inputs.shape[1], 1)

            pred_nikud[mask_nikud] = -1
            pred_dagesh[mask_dagesh] = -1
            pred_sin[mask_sin] = -1

            pred_labels = np.concatenate((pred_nikud, pred_dagesh, pred_sin),axis=2)
            if all_labels is None:
                all_labels = pred_labels
            else:
                all_labels = np.concatenate((all_labels, pred_labels), axis=0)
    return all_labels

def training(model, train_loader, dev_loader, criterion_nikud, criterion_dagesh, criterion_sin, training_params, logger,
             output_model_path, optimizer):
    best_accuracy = 0.0
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(torch.cuda.is_available())
    logger.info(f"strat training with training_params: {training_params}")
    model = model.to(device)

    criterions = {
        "nikud": criterion_nikud.to(device),
        "dagesh": criterion_dagesh.to(device),
        "sin": criterion_sin.to(device),
    }

    max_length = None
    early_stop = False
    output_checkpoints_path = os.path.join(output_model_path, "checkpoints")
    if not os.path.exists(output_checkpoints_path):
        os.makedirs(output_checkpoints_path)

    steps_loss_train_values = {"nikud": [], "dagesh": [], "sin": []}
    epochs_loss_train_values = {"nikud": [], "dagesh": [], "sin": []}
    loss_dev_values = {"nikud": [], "dagesh": [], "sin": []}
    accuracy_dev_values = {"nikud": [], "dagesh": [], "sin": [], "all_nikud_letter": [], "all_nikud_word": []}

    for epoch in tqdm(range(training_params["n_epochs"]), desc="Training"):
        if early_stop:
            logger.info('Early stopping triggered')
            break
        model.train()
        train_loss = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}
        sum = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}

        for index_data, data in enumerate(train_loader):
            (inputs, attention_mask, labels) = data
            if max_length is None:
                max_length = labels.shape[1]
            inputs = inputs.to(device)
            attention_mask = attention_mask.to(device)
            labels = labels.to(device)

            optimizer.zero_grad()
            nikud_probs, dagesh_probs, sin_probs = model(inputs, attention_mask)

            for i, (probs, name_class) in enumerate(
                    zip([nikud_probs, dagesh_probs, sin_probs], ["nikud", "dagesh", "sin"])):



                reshaped_tensor = torch.transpose(probs, 1, 2).contiguous().view(probs.shape[0],
                                                                                 probs.shape[2],
                                                                                 probs.shape[1])
                loss = criterions[name_class](reshaped_tensor, labels[:, :, i]).to(device)

                num_relevant = (labels[:, :, i] != -1).sum()
                train_loss[name_class] += loss.item() * num_relevant
                sum[name_class] += num_relevant

                loss.backward(retain_graph=True)

            for i, name_class in enumerate(["nikud", "dagesh", "sin"]):
                steps_loss_train_values[name_class].append(float(train_loss[name_class] / sum[name_class]))

            optimizer.step()
            if (index_data + 1) % 100 == 0:
                msg = f'epoch: {epoch} , index_data: {index_data + 1}\n'

                for i, name_class in enumerate(["nikud", "dagesh", "sin"]):
                    msg += f'mean loss train {name_class}: {float(train_loss[name_class] / sum[name_class])}, '
                logger.debug(msg[:-2])

        for i, name_class in enumerate(["nikud", "dagesh", "sin"]):
            epochs_loss_train_values[name_class].append(float(train_loss[name_class] / sum[name_class]))

        for name_class in train_loss.keys():
            train_loss[name_class] /= sum[name_class]

        msg = f"Epoch {epoch + 1}/{training_params['n_epochs']}\n"
        for i, name_class in enumerate(["nikud", "dagesh", "sin"]):
            msg += f'mean loss train {name_class}: {train_loss[name_class]}, '
        logger.debug(msg[:-2])

        model.eval()
        dev_loss = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}
        dev_accuracy = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}
        sum = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}
        correct_preds = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}
        masks = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}
        predictions = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}
        labels_class = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}

        all_nikud_types_correct_preds_letter = 0.0

        letter_count = 0.0
        correct_words = 0.0
        word_count = 0.0
        with torch.no_grad():
            for index_data, data in enumerate(dev_loader):
                (inputs, attention_mask, labels) = data
                inputs = inputs.to(device)
                attention_mask = attention_mask.to(device)
                labels = labels.to(device)

                nikud_probs, dagesh_probs, sin_probs = model(inputs, attention_mask)

                for i, (probs, name_class) in enumerate(
                        zip([nikud_probs, dagesh_probs, sin_probs], ["nikud", "dagesh", "sin"])):



                    reshaped_tensor = torch.transpose(probs, 1, 2).contiguous().view(probs.shape[0],
                                                                                     probs.shape[2],
                                                                                     probs.shape[1])
                    loss = criterions[name_class](reshaped_tensor, labels[:, :, i]).to(device)
                    mask = labels[:, :, i] != -1
                    num_relevant = mask.sum()
                    sum[name_class] += num_relevant
                    _, preds = torch.max(probs, 2)
                    dev_loss[name_class] += loss.item() * num_relevant
                    correct_preds[name_class] += torch.sum(preds[mask] == labels[:, :, i][mask])
                    masks[name_class] = mask
                    predictions[name_class] = preds
                    labels_class[name_class] = labels[:, :, i]


                mask_all_or = torch.logical_or(torch.logical_or(masks["nikud"], masks["dagesh"]), masks["sin"])

                correct = {name_class: (torch.ones(mask_all_or.shape) == 1).to(device) for name_class in
                           ["nikud", "dagesh", "sin"]}

                for i, name_class in enumerate(["nikud", "dagesh", "sin"]):


                    correct[name_class][masks[name_class]] = predictions[name_class][masks[name_class]] == \
                                                             labels_class[name_class][masks[name_class]]

                letter_correct_mask = torch.logical_and(
                    torch.logical_and(correct["sin"][mask_all_or], correct["dagesh"][mask_all_or]),
                    correct["nikud"][mask_all_or])
                all_nikud_types_correct_preds_letter += torch.sum(letter_correct_mask)

                # nikud_correct_preds_letter += torch.sum(correct["nikud"][mask_all_or])
                # dagesh_correct_preds_letter += torch.sum(correct["dagesh"][mask_all_or])
                # shin_correct_preds_letter += torch.sum(correct["sin"][mask_all_or])
                correct_num, total_words_num = find_num_correct_words(inputs[0].cpu(), letter_correct_mask)

                word_count += total_words_num
                correct_words += correct_num
                letter_count += mask_all_or.sum()

        for name_class in ["nikud", "dagesh", "sin"]:


            dev_loss[name_class] /= sum[name_class]
            dev_accuracy[name_class] = float(correct_preds[name_class].double() / sum[name_class])

            loss_dev_values[name_class].append(float(dev_loss[name_class]))
            accuracy_dev_values[name_class].append(float(dev_accuracy[name_class]))


        dev_all_nikud_types_accuracy_letter = float(all_nikud_types_correct_preds_letter / letter_count)



        accuracy_dev_values["all_nikud_letter"].append(dev_all_nikud_types_accuracy_letter)

        word_all_nikud_accuracy = correct_words / word_count
        accuracy_dev_values["all_nikud_word"].append(word_all_nikud_accuracy)

        msg = f"Epoch {epoch + 1}/{training_params['n_epochs']}\n" \
              f'mean loss Dev nikud: {train_loss["nikud"]}, ' \
              f'mean loss Dev dagesh: {train_loss["dagesh"]}, ' \
              f'mean loss Dev sin: {train_loss["sin"]}, ' \
              f'Dev all nikud types letter Accuracy: {dev_all_nikud_types_accuracy_letter}, ' \
              f'Dev nikud letter Accuracy: {dev_accuracy["nikud"]}, ' \
              f'Dev dagesh letter Accuracy: {dev_accuracy["dagesh"]}, ' \
              f'Dev shin letter Accuracy: {dev_accuracy["sin"]}, ' \
              f'Dev word Accuracy: {word_all_nikud_accuracy}'
        logger.debug(msg)

        if dev_all_nikud_types_accuracy_letter > best_accuracy:
            best_accuracy = dev_all_nikud_types_accuracy_letter
            best_model = {
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': loss,
            }
        # else:
        #     # If the validation loss is not decreasing
        #     epochs_no_improve += 1
        #     # If the validation loss has not decreased for the 'patience' number of epochs, stop training
        #     if epochs_no_improve == training_params['patience']:
        #         early_stop = True

        # if dev_accuracy_letter > best_accuracy:
        #     best_accuracy = dev_accuracy_letter
        #     best_model = {
        #         'epoch': epoch,
        #         'model_state_dict': model.state_dict(),
        #         'optimizer_state_dict': optimizer.state_dict(),
        #         'loss': loss,
        #     }

        if epoch % training_params["checkpoints_frequency"] == 0:
            save_checkpoint_path = os.path.join(output_checkpoints_path, f'checkpoint_model_epoch_{epoch + 1}.pth')
            checkpoint = {
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': loss,
            }
            torch.save(checkpoint["model_state_dict"],
                       save_checkpoint_path)  # save_model(model, save_model_path)  # TODO: use this function in model class

    save_model_path = os.path.join(output_model_path, 'best_model.pth')
    torch.save(best_model["model_state_dict"], save_model_path)
    return best_model, best_accuracy, epochs_loss_train_values, steps_loss_train_values, loss_dev_values, accuracy_dev_values


# TODO: Add word level acc for all kinds
def evaluate(model, test_data, debug_folder=None):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()

    true_labels = {"nikud": 0, "dagesh": 0, "sin": 0}
    predictions = {"nikud": 0, "dagesh": 0, "sin": 0}
    predicted_labels_2_report = {"nikud": 0, "dagesh": 0, "sin": 0}
    masks = {"nikud": 0, "dagesh": 0, "sin": 0}
    reports = {}
    correct_preds = {"nikud": 0, "dagesh": 0, "sin": 0}
    sum = {"nikud": 0, "dagesh": 0, "sin": 0}
    labels_class = {"nikud": 0.0, "dagesh": 0.0, "sin": 0.0}

    all_nikud_types_letter_level_correct = 0.0
    nikud_letter_level_correct = 0.0
    dagesh_letter_level_correct = 0.0
    shin_letter_level_correct = 0.0

    letter_count = 0.0
    word_count = 0.0
    correct_words = 0.0
    with torch.no_grad():
        try:
            for index_data, data in enumerate(test_data):
                if DEBUG_MODE and index_data > 100:
                    break
                (inputs, attention_mask, labels) = data

                inputs = inputs.to(device)
                attention_mask = attention_mask.to(device)
                labels = labels.to(device)

                nikud_probs, dagesh_probs, sin_probs = model(inputs, attention_mask)  # , attention_mask)

                for i, (probs, name_class) in enumerate(
                        zip([nikud_probs, dagesh_probs, sin_probs], ["nikud", "dagesh", "sin"])):
                    labels_class[name_class] = labels[:, :, i]
                    mask = labels_class[name_class] != -1
                    num_relevant = mask.sum()
                    sum[name_class] += num_relevant
                    _, preds = torch.max(probs, 2)
                    correct_preds[name_class] += torch.sum(preds[mask] == labels_class[name_class][mask])
                    predictions[name_class] = preds
                    masks[name_class] = mask
                    true_labels[name_class] = labels_class[name_class][mask].cpu().numpy()
                    predicted_labels_2_report[name_class] = preds[mask].cpu().numpy()

                mask_all_or = torch.logical_or(torch.logical_or(masks["nikud"], masks["dagesh"]), masks["sin"])

                correct_nikud = (torch.ones(mask_all_or.shape) == 1).to(device)
                correct_dagesh = (torch.ones(mask_all_or.shape) == 1).to(device)
                correct_sin = (torch.ones(mask_all_or.shape) == 1).to(device)

                correct_nikud[masks["nikud"]] = predictions["nikud"][masks["nikud"]] == labels_class["nikud"][
                    masks["nikud"]]
                correct_dagesh[masks["dagesh"]] = predictions["dagesh"][masks["dagesh"]] == labels_class["dagesh"][
                    masks["dagesh"]]
                correct_sin[masks["sin"]] = predictions["sin"][masks["sin"]] == labels_class["sin"][masks["sin"]]

                # all_nikud_types_letter_level_correct += torch.sum(
                #     torch.logical_and(torch.logical_and(correct_sin[mask_all_or], correct_dagesh[mask_all_or]),
                #                       correct_nikud[mask_all_or]))

                letter_correct_mask = torch.logical_and(
                    torch.logical_and(correct_sin[mask_all_or], correct_dagesh[mask_all_or]),
                    correct_nikud[mask_all_or])
                all_nikud_types_letter_level_correct += torch.sum(letter_correct_mask)

                # nikud_correct_preds_letter += torch.sum(correct["nikud"][mask_all_or])
                # dagesh_correct_preds_letter += torch.sum(correct["dagesh"][mask_all_or])
                # shin_correct_preds_letter += torch.sum(correct["sin"][mask_all_or])

                correct_num, total_words_num = find_num_correct_words(inputs[0].cpu(), letter_correct_mask)

                # words_end_index = np.concatenate((np.array([-1]), np.where(inputs[0].cpu() == 0)[0]))
                # is_correct_words_array = [
                #     bool(letter_correct_mask[list(range((words_end_index[s] + 1), words_end_index[s + 1]))].all()) for s in
                #     range(len(words_end_index) - 1) if words_end_index[s + 1] - (words_end_index[s] + 1) > 1]

                word_count += total_words_num  # len(is_correct_words_array)
                correct_words += correct_num  # np.array(is_correct_words_array).sum()

                letter_count += mask_all_or.sum()

                nikud_letter_level_correct += torch.sum(correct_nikud[mask_all_or])
                dagesh_letter_level_correct += torch.sum(correct_dagesh[mask_all_or])
                shin_letter_level_correct += torch.sum(correct_sin[mask_all_or])
        except:
            a=1
    for i, name in enumerate(["nikud", "dagesh", "sin"]):
        report = classification_report(true_labels[name], predicted_labels_2_report[name],
                                       output_dict=True)  # target_names=list(Nikud.label_2_id[name].keys()),

        reports[name] = report
        index_labels = np.unique(true_labels[name])
        cm = confusion_matrix(true_labels[name], predicted_labels_2_report[name], labels=index_labels)

        vowel_label = [Nikud.id_2_label[name][l] for l in index_labels]
        unique_vowels_names = [Nikud.sign_2_name[int(vowel)] for vowel in vowel_label if vowel != 'WITHOUT']
        if "WITHOUT" in vowel_label:
            unique_vowels_names += ["WITHOUT"]
        cm_df = pd.DataFrame(cm, index=unique_vowels_names, columns=unique_vowels_names)

        # Display confusion matrix
        plt.figure(figsize=(10, 8))
        sns.heatmap(cm_df, annot=True, cmap="Blues", fmt="d")
        plt.title("Confusion Matrix")
        plt.xlabel("True Label")
        plt.ylabel("Predicted Label")
        if debug_folder is None:
            plt.show()
        else:
            plt.savefig(os.path.join(debug_folder, F'Confusion_Matrix_{name}.jpg'))

    all_nikud_types_letter_level_correct = all_nikud_types_letter_level_correct / letter_count
    all_nikud_types_word_level_correct = correct_words / word_count
    nikud_letter_level_correct = nikud_letter_level_correct / letter_count
    dagesh_letter_level_correct = dagesh_letter_level_correct / letter_count
    shin_letter_level_correct = shin_letter_level_correct / letter_count
    print(f"nikud_letter_level_correct = {nikud_letter_level_correct}")
    print(f"dagesh_letter_level_correct = {dagesh_letter_level_correct}")
    print(f"shin_letter_level_correct = {shin_letter_level_correct}")
    print(f"word_level_correct = {all_nikud_types_word_level_correct}")

    return reports, all_nikud_types_word_level_correct, all_nikud_types_letter_level_correct
