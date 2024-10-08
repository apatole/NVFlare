# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
from net import Net

# (1) import nvflare client API
import nvflare.client as flare

# (optional) metrics
from nvflare.client.tracking import SummaryWriter

# (optional) set a fix place for data storage
# so we don't need to download everytime
DATASET_PATH = "/tmp/nvflare/data"

# (optional) We change to use GPU to speed things up.
# if you want to use CPU, change DEVICE="cpu"
DEVICE = "cuda:0"


# key function for FedBN
def load_state_dict_skip_bn(model, state_dict):
    new_state_dict = {k: v for k, v in state_dict.items() if "bn" not in k}
    model.load_state_dict(new_state_dict, strict=False)


def main():
    transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])

    batch_size = 4
    epochs = 2

    trainset = torchvision.datasets.CIFAR10(root=DATASET_PATH, train=True, download=True, transform=transform)
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=batch_size, shuffle=True, num_workers=8)

    testset = torchvision.datasets.CIFAR10(root=DATASET_PATH, train=False, download=True, transform=transform)
    testloader = torch.utils.data.DataLoader(testset, batch_size=batch_size, shuffle=False, num_workers=8)

    net = Net()

    # (2) initializes NVFlare client API
    flare.init()

    summary_writer = SummaryWriter()
    while flare.is_running():
        # (3) receives FLModel from NVFlare
        input_model = flare.receive()
        print(f"\n[Current Round={input_model.current_round}, Site = {flare.get_site_name()}]\n")

        # (4) loads model from NVFlare, but skip bn layers
        load_state_dict_skip_bn(net, input_model.params)

        criterion = nn.CrossEntropyLoss()
        optimizer = optim.Adam(net.parameters(), lr=0.001)

        # (optional) use GPU to speed things up
        net.to(DEVICE)
        # (optional) calculate total steps
        steps = epochs * len(trainloader)
        for epoch in range(epochs):  # loop over the dataset multiple times

            running_loss = 0.0
            for i, data in enumerate(trainloader, 0):
                # get the inputs; data is a list of [inputs, labels]
                # (optional) use GPU to speed things up
                inputs, labels = data[0].to(DEVICE), data[1].to(DEVICE)

                # zero the parameter gradients
                optimizer.zero_grad()

                # forward + backward + optimize
                outputs = net(inputs)
                loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()

                # print statistics
                running_loss += loss.item()
                if i % 2000 == 1999:  # print every 2000 mini-batches
                    print(f"[{epoch + 1}, {i + 1:5d}] loss: {running_loss / 2000:.3f}")
                    global_step = input_model.current_round * steps + epoch * len(trainloader) + i

                    summary_writer.add_scalar(tag="loss", scalar=running_loss, global_step=global_step)
                    running_loss = 0.0

        print("Finished Training")

        PATH = "./cifar_net.pth"
        torch.save(net.state_dict(), PATH)

        # (5) wraps evaluation logic into a method to re-use for
        #       evaluation on both trained and received model
        def evaluate(input_weights):
            net = Net()
            net.load_state_dict(input_weights)
            # (optional) use GPU to speed things up
            net.to(DEVICE)

            correct = 0
            total = 0
            # since we're not training, we don't need to calculate the gradients for our outputs
            with torch.no_grad():
                for data in testloader:
                    # (optional) use GPU to speed things up
                    images, labels = data[0].to(DEVICE), data[1].to(DEVICE)
                    # calculate outputs by running images through the network
                    outputs = net(images)
                    # the class with the highest energy is what we choose as prediction
                    _, predicted = torch.max(outputs.data, 1)
                    total += labels.size(0)
                    correct += (predicted == labels).sum().item()

            print(f"Accuracy of the network on the 10000 test images: {100 * correct // total} %")
            return 100 * correct // total

        # (6) evaluate on received model for model selection
        accuracy = evaluate(input_model.params)
        summary_writer.add_scalar(tag="global_model_accuracy", scalar=accuracy, global_step=input_model.current_round)
        # (7) construct trained FL model
        output_model = flare.FLModel(
            params=net.cpu().state_dict(),
            metrics={"accuracy": accuracy},
            meta={"NUM_STEPS_CURRENT_ROUND": steps},
        )
        # (8) send model back to NVFlare
        flare.send(output_model)


if __name__ == "__main__":
    main()
