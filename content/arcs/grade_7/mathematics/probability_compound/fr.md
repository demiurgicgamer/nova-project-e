# Arc Spec — Probability Compound | Math | Grade 7
**Languages:** fr
**Status:** Draft — awaiting human review before DB insertion
**Framework:** See ARC_FRAMEWORK.md — all rules apply
**Prerequisites:** probability

## Stage 1 – Hook

### Hook Story 1: Les cartes de la chance (The cards of luck)
*En France, les enfants s'amusent à tirer des cartes du jeu de carte pour prédire leur journée. Alors qu'un groupe de garçons et de filles au jardin d’enfants français fait ça, l'un d'eux est très surpris.*

**Nova:**  
Alors, vous savez, il y a eu ce petit garçon en France, un collège pas trop loin de Paris. Il jouait avec ses amis et ils ont décidé de tirer une carte pour décider qui allait choisir le jeu de la journée. Le garçon a tiré une carte et a dit : “Oh, c'est un roi, je suis chanceux!” Alors ils ont continué, et le deuxième joueur a tiré un cœur, et le troisième joueur a fait la même chose – un cœur. Mais ils ont tous les trois eu la chance de tirer un cœur ! Alors que se passait-il ? Est-ce que c'était une chance ou une combinaison ?

*Tu te demandes peut-être si c'était vraiment une coïncidence ou peut-être, le jeu n’est pas juste ?*

### Hook Story 2: Les pronostics de météo et les tirages au sort (Weather forecasts and lotteries)
*Au Québec, on a beaucoup de chance avec les pronostics de météo. L’été, les enfants comme toi s’amusent à prédire le temps – s'il va pleuvoir ou non. Tu vas voir, l’idée d’attendre la météo pour décider ce que tu vas faire ce week-end… c’est tellement amusant !*

**Nova:**  
Imagine que l’on a une machine magique qui dit : “Avec 1 chance sur 4 que ça pleuve et 3 chances sur 4 que ça fasse beau, tu dois faire un choix.” Si tu veux que ça pleuve, et que tu as besoin de choisir entre 5 activités, chacune dépend de la météo. Et pourtant, tu sais que tu peux combiner ces risques… Tu veux faire du vélo, tu veux aller au cinéma, tu veux faire du camping… Qu'est-ce que tu dois faire pour avoir plus de chances que ta journée soit réussie ? Et peut-être que c’est pas juste une question de chance ? Peut-être que tu peux calculer tes probabilités.

### Hook Summary:
**Nova:**  
Les deux histoires, on peut dire, montrent que certaines situations donnent plus qu’un seul événement… On a des probabilités multiples. Alors on apprend à calculer cela pour bien réussir — peut-être que tu peux deviner comment ça va se passer en utilisant les probabilités !


## Stage 2 – Concept

### Concept Problem: Probabilités d'événements composés simples

**Nova:**  
Tu connais déjà les probabilités simples, comme “la probabilité de tirer un cœur” ou “la chance qu’il pleuve”. Maintenant, on va combiner ces événements. C’est comme quand tu veux savoir, avec deux dés à jouer, quelle est la chance de tomber sur deux 6. C’est un événement composé, pas juste un!

**Problème:**  
Dans un jeu de cartes, on tire une carte, et on la remet dans le jeu avant d’en tirer une autre. On veut savoir la probabilité de tirer un roi, puis un cœur.

Quelle est la probabilité de tirer un roi, puis un cœur ?

A. 1/52  
B. 1/13  
C. 1/52  
D. 4/52  

**Intervention Hints:**

*Level 1:*  
Pourquoi les cartes sont-elles remises dans le paquet ?  
*Level 2:*  
Est-ce que le fait de remettre une carte change la probabilité de tirer un cœur au deuxième essai ?  
*Level 3:*  
On peut dire que la probabilité de tirer un roi = 4/52, et que la probabilité de tirer un cœur = 13/52. Donc la probabilité de combiner les deux = ?

**Réponse correcte:**  
**A. 1/52**

**Explication:**  
Comme la carte est remise dans le jeu après chaque tirage, chaque événement est indépendant, donc :
P(roi) = 4/52, P(cœur) = 13/52.  
Mais la question est “Quelle est la chance de tirer un roi **et** un cœur”, donc c’est une probabilité combinée :  
P(roi) × P(cœur) = 4/52 × 13/52 = 52/2704 = 1/52

**Nova:**  
Alors c’est pas si simple qu’il y ait deux événements… Il faut bien penser à la probabilité de chaque événement, et combiner ces probabilités, surtout quand les cartes sont remises. C’est ce qu’on appelle un *événement indépendant*.

---

## Stage 3 – Guided

### Guided Problem: Événements indépendants dans une partie de jeu de cartes

**Nova:**  
Alors qu’on a vu que la probabilité de tirer un roi et de tirer un cœur sont indépendants car les cartes sont remises, regardons un autre example.

**Problème:**  
Un jeu de cartes bien mélangé est composé de 52 cartes. On tire une carte et on la remet. On tire à nouveau une carte.

Quelle est la probabilité que les deux cartes soient des coeurs ?

A. 1/4  
B. 1/16  
C. 1/26  
D. 1/52  

**Intervention Hints:**

*Level 1:*  
Les cartes sont-elles remises après chaque tirage ?  
*Level 2:*  
Tu as 13 cœurs sur 52 cartes. Et si on tire deux fois ?  
*Level 3:*  
C’est une probabilité combinée :  
P(cœur) = 13/52  
P(cœur) = 13/52  
Donc la probabilité de deux cœurs = ?

**Réponse correcte:**  
**B. 1/16**

**Explication:**  
P(cœur) = 13/52 = 1/4  
P(cœur) = 13/52 = 1/4  
P(2 cœurs) = 1/4 × 1/4 = 1/16

**Nova:**  
Donc la probabilité de tirer deux cœurs à la suite est de 1/16, parce que chaque fois c’est le même nombre de cartes, donc on peut calculer les deux probabilités séparément, et les multiplier.

---

## Stage 4 – Practice

### Practice Problem A – Probabilités indépendantes

**Nova:**  
Tu es en train de jouer à un jeu très simple – un dé en bois à 6 faces. Tu lances le dé deux fois.  
Quelle est la probabilité de tomber sur deux 6 ?

A. 1/6  
B. 1/6  
C. 1/36  
D. 2/6  

**Intervention Hints:**

*Level 1:*  
Un dé à 6 faces, combien de façons différentes à tomber ?  
*Level 2:*  
Quelle est la probabilité de tomber sur un 6 ?  
*Level 3:*  
P(6) = 1/6  
P(6) = 1/6  
Donc P(6 et 6) = ?

**Réponse correcte:**  
**C. 1/36**

**Explication:**  
P(6) = 1/6, P(6) = 1/6  
P(6 et 6) = 1/6 × 1/6 = 1/36

---

### Practice Problem B – Combiner probabilités d’événements dépendants

**Nova:**  
Maintenant, une situation différente ! On va faire un tirage sans remise.  
Tu tires une carte du jeu, et tu ne la remets pas.

**Problème:**  
On fait un tirage de deux cartes dans un jeu de 52 cartes, sans remise.

Quelle est la probabilité de tirer deux cartes de cœur ?

A. 1/36  
B. 1/26  
C. 1/13  
D. 1/4  

**Intervention Hints:**

*Level 1:*  
Le jeu ne remet pas les cartes. Est-ce que ça change la probabilité ?  
*Level 2:*  
La probabilité de la première carte est 13/52.  
Quelle est la probabilité de la deuxième ?  
*Level 3:*  
P(cœur) = 13/52  
P(deuxième cœur) = 12/51  
P(2 cœurs) = ?

**Réponse correcte:**  
**B. 1/26**

**Explication:**  
P(cœur) = 13/52  
P(deuxième cœur) = 12/51  
P(2 cœurs) = 13/52 × 12/51 = 156/2652 = 1/26

---

### Practice Problem C – Probabilités combinées dans un tirage à la loterie

**Nova:**  
On va imaginer que tu gagnes une loterie d’été, et tu dois tirer un numéro au sort. Tu as 30 billets et tu en choisis un.  
Quelle est la probabilité de tirer ton numéro si tu as 3 chances sur 10 dans chaque tirage ?

**Problème:**  
Tu as 10 billets dans une loterie de 30 billets.  
Quelle est la chance que ta numérotation ait été tirée deux fois consécutivement ?

A. 3/10  
B. 9/100  
C. 6/10  
D. 9/10  

**Intervention Hints:**

*Level 1:*  
Tu tires un billet au sort : combien de chances de tomber sur ton numéro ?  
*Level 2:*  
Si les tirages sont indépendants, qu’est-ce que tu peux faire ?  
*Level 3:*  
P(tirer ton numéro) = 10/30 = 1/3  
P(même numéro deux fois) = ?

**Réponse correcte:**  
**B. 9/100**

**Explication:**  
P(tirer ton numéro) = 10/30 = 1/3  
P(tirer ton numéro deux fois) = 1/3 × 1/3 = 1/9  
Mais dans le problème, c’était "3 chances sur 10" = 0.3  
Donc: 0.3 × 0.3 = 0.09 → 9/100

---

## Stage 5 – Culminating

### Culminating Challenge Problem:

**Nova:**  
Tu fais partie d’un groupe de 5 enfants dans un camp de jeux. Vous jouez un jeu de dés, et vous avez deux dés. Chacun veut choisir le type de point qui est le plus probable quand vous lancez les deux dés.

**Problème:**  
Dans une partie de jeu, vous jouez avec deux dés, et vous souhaitez que la somme des deux dés vous donne 6. Quelle est la probabilité qu’un lancer vous donne une somme de 6?

A. 1/36  
B. 1/12  
C. 5/36  
D. 1/6  

**Intervention Hints:**

*Level 1:*  
Combien de combinaisons possible dans deux dés à 6 faces ?  
*Level 2:*  
Comment faire pour obtenir une somme de 6 ?  
*Level 3:*  
Sommations possibles = 1+5, 2+4, 3+3, 4+2, 5+1  
Donc 5 combinaisons possibles sur 36 = ?

**Réponse correcte:**  
**C. 5/36**

**Explication:**  
Les combinaisons pour faire 6 = (1,5), (2,4), (3,3), (4,2), (5,1) = 5 façons.  
Total des combinaisons = 6 × 6 = 36  
P(somme = 6) = 5/36

**Nova:**  
C’est donc ça – avec des événements composés, il faut aussi voir combien de façons les choses peuvent se combiner. Et tu peux même les calculer sans avoir à tester.  
Bonne chance, et continue à explorer les probabilités du monde réel !


---


## Révision des compétences

| Compétence | Comment évaluer ? |
|------------|-------------------|
| Calculer des probabilités composées | Utiliser des problèmes sur des événements indépendants |
| Distinguer les événements indépendants ou dépendants | Répondre à des situations avec et sans remise |
| Comprendre les combinaisons de probabilités | Utiliser des exemples concrets comme les dés ou les cartes |
| Appliquer une méthode de probabilité par multiplication | Identifier les probabilités simples, puis multipliées |

> **Conclusion :**  
Les probabilités composées ne sont pas aussi complexes qu’elles peuvent sembler. En comprenant si chaque événement est indépendant ou non, et en multipliant les probabilités simples, tu peux résoudre de nombreux problèmes de vraie vie. Continue d'explorer et de jouer avec les mathématiques !