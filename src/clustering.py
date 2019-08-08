import re
from math import sqrt
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
import spacy
import torch
import torch.nn as nn
from pandarallel import pandarallel
from sklearn.preprocessing import MultiLabelBinarizer
from torch.autograd import Variable
from torch.nn import init
from torch.nn.functional import gumbel_softmax
from torch.optim import SGD

############################### CONSTANTS ###############################
scilens_dir = str(Path.home()) + '/data/scilens/cache/diffusion_graph/scilens_3M/'
sciclops_dir = str(Path.home()) + '/data/sciclops/'

nlp = spacy.load("en_core_sci_md")

############################### ######### ###############################

################################ HELPERS ################################

#Read diffusion graph
def read_graph(graph_file):
		return nx.from_pandas_edgelist(pd.read_csv(graph_file, sep='\t', header=None), 0, 1, create_using=nx.DiGraph())


def data_preprocessing(use_cache=True):
	if use_cache:
		cooc = pd.read_csv(sciclops_dir + 'cache/cooc.tsv.bz2', sep='\t', index_col='url')
		articles_vec = pd.read_csv(sciclops_dir + 'cache/articles_vec.tsv.bz2', sep='\t', index_col='url')
		papers_vec = pd.read_csv(sciclops_dir + 'cache/papers_vec.tsv.bz2', sep='\t', index_col='url')

	else:
		pandarallel.initialize()
		
		articles = pd.read_csv(scilens_dir + 'article_details_v2.tsv.bz2', sep='\t')
		papers = pd.read_csv(scilens_dir + 'paper_details_v1.tsv.bz2', sep='\t')
		G = read_graph(scilens_dir + 'diffusion_graph_v7.tsv.bz2')
		articles['refs'] = articles.url.parallel_apply(lambda u: set(G[u]))
		articles = articles.set_index('url')
		papers = papers.set_index('url')

		#cleaning
		print('cleaning...')
		blacklist_refs  = set(open(sciclops_dir + 'blacklist/sources.txt').read().splitlines())
		articles['refs'] = articles.refs.parallel_apply(lambda r: (r - blacklist_refs).intersection(set(papers.index.to_list())))
		mlb = MultiLabelBinarizer()
		cooc = pd.DataFrame(mlb.fit_transform(articles.refs), columns=mlb.classes_, index=articles.index)
		papers = papers[papers.index.isin(list(cooc.columns))]
		articles.title = articles.title.astype(str)
		articles.full_text = articles.full_text.astype(str)
		papers.title = papers.title.astype(str)
		papers.full_text = papers.full_text.astype(str)
		
		print('vectorizing...')
		articles_vec = articles.parallel_apply(lambda x: nlp(x['title'] + ' ' + x['full_text']).vector , axis=1).apply(pd.Series)
		papers_vec = papers.parallel_apply(lambda x: nlp(x['title'] + ' ' + x['full_text']).vector , axis=1).apply(pd.Series)

		#caching    
		cooc.to_csv(sciclops_dir + 'cache/cooc.tsv', sep='\t')
		articles_vec.to_csv(sciclops_dir + 'cache/articles_vec.tsv', sep='\t')
		papers_vec.to_csv(sciclops_dir + 'cache/papers_vec.tsv', sep='\t')
	
	cooc = torch.Tensor(cooc.values.astype(float))
	articles_vec = torch.Tensor(articles_vec.values.astype(float))
	papers_vec = torch.Tensor(papers_vec.values.astype(float))
	
	return cooc, articles_vec, papers_vec


############################### ######### ###############################

cooc, articles_vec, papers_vec = data_preprocessing()

# Hyper Parameters
num_epochs = 50
learning_rate = 1.e-6
weight_decay = 1.e-5

hard_clustering_articles = True
hard_clustering_papers = False
num_clusters = 10
linear_comb = 0.6
hidden_layers = 100

class ClusterNet(nn.Module):
	def __init__(self, num_clusters, num_articles, num_papers, embeddings_dim=200):
		super(ClusterNet, self).__init__()

		self.num_articles = num_articles
		self.num_papers = num_papers
		self.num_clusters = num_clusters
		self.avg_articles_per_cluster = self.num_articles/self.num_clusters
		self.avg_papers_per_cluster = self.num_papers/self.num_clusters

		self.linear_2_level = nn.Sequential(
        	nn.Linear(embeddings_dim, hidden_layers),
			nn.BatchNorm1d(hidden_layers),
			nn.ReLU(),
			nn.Linear(hidden_layers, num_clusters),
			nn.Softmax(dim=1)
        )
		self.linear_1_level = nn.Sequential(
        	nn.Linear(embeddings_dim, num_clusters),
			#nn.BatchNorm1d(num_clusters),
			nn.Softmax(dim=1)
        )

	def forward(self, articles, papers, cooc):
		A = self.linear_1_level(articles)
		P = self.linear_1_level(papers)
		C = cooc
		return A, P, C
	

	def loss(self, A, P, C):
		D = A.t() @ C @ P

		cluster_spread_loss = torch.sum(torch.sum(torch.tril(D, diagonal=-1), dim=0) + torch.sum(torch.triu(D, diagonal=1), dim=0))
		balance_loss = torch.sum((torch.sum(A, dim=0) - self.avg_articles_per_cluster)**2)
		
		#print(cluster_spread_loss, balance_loss)
		return linear_comb * cluster_spread_loss + (1-linear_comb) * balance_loss
		

#Model training
model = ClusterNet(num_clusters, len(articles_vec), len(papers_vec))
optimizer = SGD(model.parameters(), lr=learning_rate, weight_decay=weight_decay) 

for epoch in range(num_epochs):    
	optimizer.zero_grad()
	A, P, C = model(articles_vec, papers_vec, cooc)
	loss = model.loss(A, P, C)
	print(loss.data.item())
	loss.backward()
	optimizer.step()




articles = pd.read_csv(scilens_dir + 'article_details_v2.tsv.bz2', sep='\t')
[u for u in pd.DataFrame(A[:,5].data.tolist()).nlargest(5, 0).join(articles)['url']]