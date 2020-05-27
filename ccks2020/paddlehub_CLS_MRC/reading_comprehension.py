#coding:utf-8
#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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
"""Finetuning on reading comprehension task """

import argparse
import ast
import json
import os
from paddlehub.common.logger import logger
import paddle.fluid as fluid
import paddlehub as hub

from demo_dataset import DuReader

# yapf: disable
parser = argparse.ArgumentParser(__doc__)
parser.add_argument("--seed", type=int, default=1666, help="Number of epoches for fine-tuning.")
parser.add_argument("--num_epoch", type=int, default=1, help="Number of epoches for fine-tuning.")
parser.add_argument("--use_gpu", type=ast.literal_eval, default=True, help="Whether use GPU for finetuning, input should be True or False")
parser.add_argument("--learning_rate", type=float, default=3e-5, help="Learning rate used to train with warmup.")
parser.add_argument("--weight_decay", type=float, default=0.01, help="Weight decay rate for L2 regularizer.")
parser.add_argument("--warmup_proportion", type=float, default=0.0, help="Warmup proportion params for warmup strategy")
parser.add_argument("--checkpoint_dir", type=str, default='model_mrc/', help="Directory to model checkpoint")
parser.add_argument("--model", type=str, default='ernie', help="Directory to model checkpoint")
parser.add_argument("--max_seq_len", type=int, default=203, help="Number of words of the longest seqence.")
parser.add_argument("--max_que_len", type=int, default=16, help="Number of words of the longest seqence.")
parser.add_argument("--batch_size", type=int, default=8, help="Total examples' number in batch for training.")
parser.add_argument("--use_data_parallel", type=ast.literal_eval, default=True, help="Whether use data parallel.")

# yapf: enable.
# 重构train_log和eval_log时的事件，
# 1.增加visualdl
# 2.修改以eval_loss保存最好模型，
# 3.eval保存的模型不再是推断模型，而是与step时一样的训练模型
def change_task(task,id):
    def new_log_interval_event(self, run_states):
        scores, avg_loss, run_speed = self._calculate_metrics(run_states)
        self.tb_writer.add_scalar(
            tag="Loss_{}".format(self.phase),
            scalar_value=avg_loss,
            global_step=self._envs['train'].current_step)
        log_scores = ""
        log=[self._envs['train'].current_step,avg_loss]
        for metric in scores:
            self.tb_writer.add_scalar(
                tag="{}_{}".format(metric, self.phase),
                scalar_value=scores[metric],
                global_step=self._envs['train'].current_step)
            log_scores += "%s=%.5f " % (metric, scores[metric])
            log.append(scores[metric])
        logger.train("step %d / %d: loss=%.5f %s[step/sec: %.2f]" %
                     (self.current_step, self.max_train_steps, avg_loss,
                      log_scores, run_speed))
        with open('./work/event/MRC_log_{}train.txt'.format(id), 'a', encoding='utf-8') as f:
            f.write(','.join(log) + '\n')

    # def new_run_step_event(self,run_states):
    def new_eval_end_event(self, run_states):
        """
        Paddlehub default handler for eval_end_event, it will complete visualization and metrics calculation
        Args:
            run_states (object): the results in eval phase
        """
        eval_scores, eval_loss, run_speed = self._calculate_metrics(run_states)
        log=[]
        if 'train' in self._envs:
            self.tb_writer.add_scalar(
                tag="Loss_{}".format(self.phase),
                scalar_value=eval_loss,
                global_step=self._envs['train'].current_step)
            log=[self._envs['train'].current_step]

        log_scores = ""

        log.append(eval_loss)
        for metric in eval_scores:
            if 'train' in self._envs:
                self.tb_writer.add_scalar(
                    tag="{}_{}".format(metric, self.phase),
                    scalar_value=eval_scores[metric],
                    global_step=self._envs['train'].current_step)
            log_scores += "%s=%.5f " % (metric, eval_scores[metric])
            log.append(eval_scores[metric])
        logger.eval(
            "[%s dataset evaluation result] loss=%.5f %s[step/sec: %.2f]" %
            (self.phase, eval_loss, log_scores, run_speed))
        with open('./work/event/MRC_log_{}dev.txt'.format(id),'a',encoding='utf-8') as f:
            f.write(','.join(log)+'\n')

        eval_scores_items = eval_scores.items()
        if len(eval_scores_items):
            # The first metric will be chose to eval
            main_metric, main_value = list(eval_scores_items)[0]
        else:
            logger.warning(
                "None of metrics has been implemented, loss will be used to evaluate."
            )
            # The larger, the better
            main_metric, main_value = "negative loss", -eval_loss
        if self.phase in ["dev", "val"] and main_value > self.best_score:
            self.best_score = main_value
            model_saved_dir = os.path.join(self.config.checkpoint_dir,
                                           "best_model")
            logger.eval("best model saved to %s [best %s=%.5f]" %
                        (model_saved_dir, main_metric, main_value))
            self.save_inference_model(dirname=model_saved_dir)



    # name：hook名字，“default”表示PaddleHub内置_log_interval_event实现
    task.delete_hook(hook_type="eval_end_event", name="default")
    task.delete_hook(hook_type="log_interval_event", name="default")
    task.add_hook(hook_type="eval_end_event", name="new_eval_end_event", func=new_eval_end_event)
    task.add_hook(hook_type="log_interval_event", name="new_log_interval_event", func=new_log_interval_event)
    return task

def one(id,train_i,args):
    # 加载PaddleHub ERNIE预训练模型
    module = hub.Module(name=args.model)

    # ERNIE预训练模型输入变量inputs、输出变量outputs、以及模型program
    inputs, outputs, program = module.context(
        trainable=True, max_seq_len=args.max_seq_len)

    # 加载竞赛数据集并使用ReadingComprehensionReader读取数据
    dataset = DuReader(id)
    reader = hub.reader.ReadingComprehensionReader(
        dataset=dataset,
        vocab_path=module.get_vocab_path(),
        max_seq_len=args.max_seq_len,
        doc_stride=128,
        max_query_length=args.max_que_len)

    # 取ERNIE的字级别预训练输出
    seq_output = outputs["sequence_output"]

    # 设置运行program所需的feed_list
    feed_list = [
        inputs["input_ids"].name,
        inputs["position_ids"].name,
        inputs["segment_ids"].name,
        inputs["input_mask"].name,
    ]

    # 选择Fine-tune优化策略
    strategy = hub.AdamWeightDecayStrategy(
        weight_decay=args.weight_decay,
        learning_rate=args.learning_rate,
        warmup_proportion=args.warmup_proportion)

    # 设置运行配置
    config = hub.RunConfig(
        eval_interval=200,
        use_pyreader=False,
        use_data_parallel=args.use_data_parallel,
        use_cuda=args.use_gpu,
        num_epoch=args.num_epoch,
        batch_size=args.batch_size,
        checkpoint_dir=args.checkpoint_dir+str(id),
        strategy=strategy)

    # 定义阅读理解Fine-tune Task
    # 由于竞赛数据集与cmrc2018数据集格式比较相似，此处sub_task应为cmrc2018
    # 否则运行可能出错
    reading_comprehension_task = hub.ReadingComprehensionTask(
        data_reader=reader,
        feature=seq_output,
        feed_list=feed_list,
        config=config,
        sub_task="cmrc2018",
    )
    reading_comprehension_task.main_program.random_seed = args.seed
    change_task(reading_comprehension_task, id)
    # 调用finetune_and_eval API，将会自动进行训练、评估以及保存最佳模型
    reading_comprehension_task.finetune_and_eval()

    # 竞赛数据集测试集部分数据用于预测
    data = dataset.predict_examples
    # 调用predict接口, 打开return_result(True)，将自动返回预测结果
    all_prediction = reading_comprehension_task.predict(data=data, return_result=True)
    # 写入预测结果
    json.dump(all_prediction, open('./work/result/submit{}_{}.json'.format(train_i,id), 'w'), ensure_ascii=False)
    value = [id,reading_comprehension_task.best_score]+list(args.__dict__.values())
    value = [str(x) for x in value]
    with open('./work/log/MRC_log.txt', 'a', encoding='utf-8') as f:
        f.write(','.join(value)+'\n')
    return reading_comprehension_task.best_score,value[2:]

if __name__ == '__main__':
    id=train_i=0
    args = parser.parse_args()
    title = ['id', 'score'] + list(args.__dict__.keys())
    with open('./work/log/MRC_log.txt', 'a', encoding='utf-8') as f:
        f.write(','.join(title)+'\n')
    one(id,train_i,args)

